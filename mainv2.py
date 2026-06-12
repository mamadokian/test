#!/usr/bin/env python3
"""
Concurrent Lua Branch Sync - with coordinated rate limiting
Prevents all threads from sleeping simultaneously by sharing rate limit state.
"""

import requests
import base64
import json
import time
import os
import sys
import argparse
import concurrent.futures
import threading
from typing import Set, Optional, List
from datetime import datetime


class RateLimitManager:
    """Shared rate limit state across all threads."""
    def __init__(self):
        self.lock = threading.Lock()
        self.remaining = 5000
        self.reset_time = 0
        self.sleeping_threads = 0
    
    def update(self, response: requests.Response):
        with self.lock:
            self.remaining = int(response.headers.get('X-RateLimit-Remaining', self.remaining))
            self.reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
    
    def check_and_wait(self):
        """
        Check if we need to sleep. Only one thread sleeps at a time,
        others wait for the wake signal.
        """
        with self.lock:
            if self.remaining > 5:
                self.remaining -= 1  # Reserve one for this request
                return False  # No sleep needed
            
            # Need to sleep
            now = time.time()
            sleep_time = max(self.reset_time - now, 0) + 2
            
            if sleep_time <= 0:
                self.remaining = 5000  # Reset should have happened
                return False
            
            self.sleeping_threads += 1
            thread_num = self.sleeping_threads
        
        # Sleep outside the lock so other threads can see the state
        print(f"  ⏳ [{thread_num}] Rate limit exhausted. Sleeping {sleep_time:.0f}s until reset...")
        time.sleep(sleep_time)
        
        with self.lock:
            self.sleeping_threads -= 1
            self.remaining = 5000  # Assume refreshed after sleep
        
        return True


class LuaBranchSync:
    def __init__(self, token: str, output_dir: str = ".", cache_hours: int = 24, workers: int = 50):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
            "User-Agent": "LuaSync/1.0"
        })
        self.output_dir = output_dir
        self.cache_hours = cache_hours
        self.workers = workers
        self.rate_limit = RateLimitManager()
        os.makedirs(output_dir, exist_ok=True)
        
        self.progress_file = os.path.join(output_dir, "lua_sync_progress.json")
        self.lock = threading.Lock()

    def _get(self, url: str, params: dict = None) -> requests.Response:
        while True:
            self.rate_limit.check_and_wait()
            resp = self.session.get(url, params=params or {}, timeout=30)
            self.rate_limit.update(resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                continue  # Will sleep on next loop
            if resp.status_code == 404:
                return resp
            resp.raise_for_status()
            return resp

    def _post(self, url: str, json_data: dict) -> requests.Response:
        while True:
            self.rate_limit.check_and_wait()
            resp = self.session.post(url, json=json_data, timeout=30)
            self.rate_limit.update(resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                continue
            return resp

    def _put(self, url: str, json_data: dict) -> requests.Response:
        while True:
            self.rate_limit.check_and_wait()
            resp = self.session.put(url, json=json_data, timeout=30)
            self.rate_limit.update(resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                continue
            return resp

    def load_progress(self) -> dict:
        if os.path.exists(self.progress_file):
            with open(self.progress_file, "r") as f:
                return json.load(f)
        return {"completed": [], "failed": []}

    def save_progress(self, progress: dict):
        with self.lock:
            with open(self.progress_file, "w") as f:
                json.dump(progress, f, indent=2)

    def load_branch_cache(self, owner: str, repo: str) -> Optional[Set[str]]:
        path = os.path.join(self.output_dir, f"{owner}_{repo}_branches.json")
        if not os.path.exists(path):
            return None
        if (time.time() - os.path.getmtime(path)) / 3600 > self.cache_hours:
            return None
        with open(path, "r") as f:
            return set(json.load(f).get("branches", []))

    def fetch_branches(self, owner: str, repo: str) -> Set[str]:
        branches = set()
        page = 1
        print(f"Fetching branches from {owner}/{repo}...")
        
        while True:
            resp = self._get(f"https://api.github.com/repos/{owner}/{repo}/branches", 
                           {"per_page": 100, "page": page})
            data = resp.json()
            if not data:
                break
            for b in data:
                branches.add(b["name"])
            if len(data) < 100:
                break
            page += 1
            if page % 10 == 0:
                print(f"  ... {len(branches)} branches")
        
        with open(os.path.join(self.output_dir, f"{owner}_{repo}_branches.json"), "w") as f:
            json.dump({
                "repository": f"{owner}/{repo}",
                "total_count": len(branches),
                "branches": sorted(list(branches)),
                "fetched_at": datetime.utcnow().isoformat() + "Z"
            }, f, indent=2)
        
        print(f"  ✓ Cached {len(branches)} branches")
        return branches

    def get_branches(self, owner: str, repo: str) -> Set[str]:
        cached = self.load_branch_cache(owner, repo)
        return cached if cached is not None else self.fetch_branches(owner, repo)

    def load_missing_branches(self, filepath: str) -> Set[str]:
        print(f"Loading missing branches from {filepath}...")
        with open(filepath, "r") as f:
            data = json.load(f)
        branches = set(data.get("missing_branches", []))
        print(f"  ✓ Loaded {len(branches)} missing branches")
        return branches

    def get_lua_content(self, owner: str, repo: str, branch: str) -> Optional[str]:
        path = f"{branch}.lua"
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        resp = self._get(url, {"ref": branch})
        
        if resp.status_code == 404:
            return None
        
        data = resp.json()
        
        if data.get("encoding") == "base64" and data.get("content"):
            return data["content"].replace("\n", "")
        
        if data.get("download_url"):
            raw = self.session.get(data["download_url"], timeout=30)
            raw.raise_for_status()
            return base64.b64encode(raw.content).decode()
        
        return None

    def get_default_branch_sha(self, owner: str, repo: str) -> str:
        resp = self._get(f"https://api.github.com/repos/{owner}/{repo}")
        default = resp.json().get("default_branch", "main")
        ref = self._get(f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{default}")
        return ref.json()["object"]["sha"]

    def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        resp = self.session.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}",
            timeout=10
        )
        self.rate_limit.update(resp)
        return resp.status_code == 200

    def create_branch(self, owner: str, repo: str, branch: str, base_sha: str) -> bool:
        resp = self._post(
            f"https://api.github.com/repos/{owner}/{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha}
        )
        if resp.status_code == 201:
            return True
        if resp.status_code == 422 and "already exists" in resp.text:
            return True
        return False

    def upload_lua(self, owner: str, repo: str, branch: str, content_b64: str) -> bool:
        path = f"{branch}.lua"
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        
        existing_sha = None
        check = self.session.get(url, params={"ref": branch}, timeout=10)
        self.rate_limit.update(check)
        if check.status_code == 200:
            existing_sha = check.json().get("sha")
        
        payload = {
            "message": f"Add {path}",
            "content": content_b64,
            "branch": branch
        }
        if existing_sha:
            payload["sha"] = existing_sha
        
        resp = self._put(url, payload)
        return resp.status_code in (200, 201)

    def sync_one_branch(self, args: tuple) -> tuple:
        src_owner, src_repo, tgt_owner, tgt_repo, branch, tgt_base_sha = args
        
        try:
            content = self.get_lua_content(src_owner, src_repo, branch)
            if content is None:
                return branch, False, "lua_not_found"
            
            if not self.branch_exists(tgt_owner, tgt_repo, branch):
                if not self.create_branch(tgt_owner, tgt_repo, branch, tgt_base_sha):
                    return branch, False, "create_branch_failed"
            
            if not self.upload_lua(tgt_owner, tgt_repo, branch, content):
                return branch, False, "upload_failed"
            
            return branch, True, "ok"
            
        except Exception as e:
            return branch, False, str(e)

    def run(self, source: str, target: str, missing_file: str = None, dry_run: bool = False):
        src_owner, src_repo = source.split("/")
        tgt_owner, tgt_repo = target.split("/")
        
        if missing_file:
            missing = sorted(self.load_missing_branches(missing_file))
            print(f"\n{'='*60}")
            print(f"Using missing_branches file: {len(missing):,} branches")
            print(f"{'='*60}\n")
        else:
            print(f"\n[Source] {source}")
            source_branches = self.get_branches(src_owner, src_repo)
            
            print(f"\n[Target] {target}")
            target_branches = self.get_branches(tgt_owner, tgt_repo)
            
            missing = sorted(source_branches - target_branches)
            print(f"\n{'='*60}")
            print(f"Source: {len(source_branches):,} | Target: {len(target_branches):,} | Missing: {len(missing):,}")
            print(f"{'='*60}\n")
        
        if not missing:
            print("✓ Nothing to sync!")
            return
        
        if dry_run:
            print(f"[DRY RUN] Would sync {len(missing)} branches with {self.workers} workers")
            return
        
        print("Getting target base SHA...")
        tgt_base_sha = self.get_default_branch_sha(tgt_owner, tgt_repo)
        print(f"  Base: {tgt_base_sha[:7]}...\n")
        
        progress = self.load_progress()
        completed = set(progress.get("completed", []))
        failed = list(progress.get("failed", []))
        
        to_process = [b for b in missing if b not in completed]
        print(f"Resume: {len(completed)} done, {len(to_process)} remaining")
        print(f"Workers: {self.workers}")
        print(f"Rate limit: 5,000/hour shared across all threads\n")
        
        worker_args = [
            (src_owner, src_repo, tgt_owner, tgt_repo, branch, tgt_base_sha)
            for branch in to_process
        ]
        
        completed_new = 0
        failed_new = 0
        processed_since_save = 0
        
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                future_to_branch = {
                    executor.submit(self.sync_one_branch, args): args[4] 
                    for args in worker_args
                }
                
                for future in concurrent.futures.as_completed(future_to_branch):
                    branch = future_to_branch[future]
                    try:
                        _, success, reason = future.result()
                    except Exception as e:
                        success, reason = False, str(e)
                    
                    with self.lock:
                        if success:
                            completed.add(branch)
                            completed_new += 1
                            print(f"  ✓ {branch}")
                        else:
                            failed.append({"branch": branch, "reason": reason})
                            failed_new += 1
                            print(f"  ✗ {branch}: {reason}")
                        
                        processed_since_save += 1
                        
                        if processed_since_save >= 50:
                            self.save_progress({
                                "completed": sorted(list(completed)), 
                                "failed": failed
                            })
                            print(f"  💾 Progress saved ({len(completed)}/{len(missing)} total)")
                            processed_since_save = 0
                        
        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted.")
        finally:
            self.save_progress({
                "completed": sorted(list(completed)), 
                "failed": failed
            })
        
        print(f"\n{'='*60}")
        print("DONE")
        print(f"Total completed: {len(completed)}")
        print(f"Total failed:    {len(failed)}")
        print(f"This run:        +{completed_new} done, +{failed_new} failed")
        print(f"Progress:        {self.progress_file}")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Concurrent .lua branch sync with coordinated rate limiting."
    )
    parser.add_argument("source", help="Source repo (owner/repo)")
    parser.add_argument("target", help="Target repo (owner/repo)")
    parser.add_argument("--token", "-t", required=True, help="GitHub token")
    parser.add_argument("--missing-file", "-m", help="Path to missing_branches JSON")
    parser.add_argument("--output-dir", "-o", default=".", help="Cache/progress directory")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Preview only")
    parser.add_argument("--cache-hours", "-c", type=int, default=24, help="Branch cache max age")
    parser.add_argument("--workers", "-w", type=int, default=50, help="Concurrent threads")
    
    args = parser.parse_args()
    
    sync = LuaBranchSync(
        token=args.token, 
        output_dir=args.output_dir, 
        cache_hours=args.cache_hours,
        workers=args.workers
    )
    
    print("=" * 60)
    print("Concurrent Lua Branch Sync (Coordinated Rate Limit)")
    print("=" * 60)
    print(f"Source:  {args.source}")
    print(f"Target:  {args.target}")
    print(f"Workers: {args.workers}")
    print(f"Cache:   {args.cache_hours}h")
    print(f"{'='*60}")
    
    sync.run(args.source, args.target, missing_file=args.missing_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
