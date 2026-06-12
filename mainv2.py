#!/usr/bin/env python3
"""
Multi-Token Concurrent Lua Branch Sync
Backs up missing_branches.json AND sync_progress.json to repo2 main branch.
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


class TokenPool:
    def __init__(self, tokens: List[str]):
        self.tokens = tokens
        self.sessions = []
        self.rate_limits = []
        self.locks = []
        
        for token in tokens:
            session = requests.Session()
            session.headers.update({
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"token {token}",
                "User-Agent": "LuaSync/1.0"
            })
            self.sessions.append(session)
            self.rate_limits.append({"remaining": 5000, "reset_time": 0})
            self.locks.append(threading.Lock())
        
        self.token_count = len(tokens)
        print(f"Initialized token pool with {self.token_count} token(s)")
        print(f"Total budget: ~{self.token_count * 5000:,} requests/hour")
    
    def get_session(self, token_index: int) -> requests.Session:
        return self.sessions[token_index]
    
    def update_rate_limit(self, token_index: int, response: requests.Response):
        with self.locks[token_index]:
            self.rate_limits[token_index]["remaining"] = int(
                response.headers.get('X-RateLimit-Remaining', 5000)
            )
            self.rate_limits[token_index]["reset_time"] = int(
                response.headers.get('X-RateLimit-Reset', 0)
            )
    
    def find_available_token(self) -> int:
        while True:
            best_idx = -1
            best_remaining = -1
            
            for i in range(self.token_count):
                with self.locks[i]:
                    remaining = self.rate_limits[i]["remaining"]
                    reset_time = self.rate_limits[i]["reset_time"]
                    
                    if reset_time > 0 and time.time() > reset_time + 1:
                        self.rate_limits[i]["remaining"] = 5000
                        remaining = 5000
                    
                    if remaining > best_remaining:
                        best_remaining = remaining
                        best_idx = i
            
            if best_remaining > 3:
                with self.locks[best_idx]:
                    self.rate_limits[best_idx]["remaining"] -= 1
                return best_idx
            
            earliest_reset = min(
                self.rate_limits[i]["reset_time"] 
                for i in range(self.token_count)
            )
            sleep_time = max(earliest_reset - time.time(), 0) + 2
            
            print(f"  ⏳ ALL {self.token_count} token(s) exhausted. Sleeping {sleep_time:.0f}s...")
            time.sleep(sleep_time)
            
            for i in range(self.token_count):
                with self.locks[i]:
                    if time.time() > self.rate_limits[i]["reset_time"]:
                        self.rate_limits[i]["remaining"] = 5000


class LuaBranchSync:
    MISSING_FILENAME = "missing_branches.json"
    PROGRESS_FILENAME = "sync_progress.json"
    
    def __init__(self, tokens: List[str], output_dir: str = ".", cache_hours: int = 24, workers: int = 50):
        self.token_pool = TokenPool(tokens)
        self.output_dir = output_dir
        self.cache_hours = cache_hours
        self.workers = workers
        os.makedirs(output_dir, exist_ok=True)
        
        self.progress_file = os.path.join(output_dir, "lua_sync_progress.json")
        self.lock = threading.Lock()

    def _request(self, method: str, url: str, **kwargs):
        while True:
            idx = self.token_pool.find_available_token()
            session = self.token_pool.get_session(idx)
            
            if method == "GET":
                resp = session.get(url, timeout=30, **kwargs)
            elif method == "POST":
                resp = session.post(url, timeout=30, **kwargs)
            elif method == "PUT":
                resp = session.put(url, timeout=30, **kwargs)
            else:
                raise ValueError(f"Unknown method: {method}")
            
            self.token_pool.update_rate_limit(idx, resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                continue
            
            return resp, idx

    def _get(self, url: str, params: dict = None) -> requests.Response:
        resp, _ = self._request("GET", url, params=params or {})
        if resp.status_code == 404:
            return resp
        resp.raise_for_status()
        return resp

    def _post(self, url: str, json_data: dict) -> requests.Response:
        resp, _ = self._request("POST", url, json=json_data)
        return resp

    def _put(self, url: str, json_data: dict) -> requests.Response:
        resp, _ = self._request("PUT", url, json=json_data)
        return resp

    def load_progress(self) -> dict:
        # Try local first
        if os.path.exists(self.progress_file):
            with open(self.progress_file, "r") as f:
                return json.load(f)
        return {"completed": [], "failed": []}

    def save_progress(self, progress: dict):
        with self.lock:
            with open(self.progress_file, "w") as f:
                json.dump(progress, f, indent=2)

    def pull_json_from_repo(self, owner: str, repo: str, filename: str, branch: str = "main") -> Optional[dict]:
        """Pull any JSON file from repo's main branch."""
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}"
        print(f"Checking for {filename} in {owner}/{repo}:{branch}...")
        
        try:
            resp = self._get(url, {"ref": branch})
            if resp.status_code == 404:
                print(f"  ✗ Not found in repo")
                return None
            
            data = resp.json()
            if data.get("encoding") == "base64" and data.get("content"):
                content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
                parsed = json.loads(content)
                print(f"  ✓ Found! {len(str(parsed))} bytes")
                return parsed
            
            print(f"  ✗ File exists but couldn't decode")
            return None
            
        except Exception as e:
            print(f"  ✗ Error reading from repo: {e}")
            return None

    def push_json_to_repo(self, owner: str, repo: str, filename: str, data: dict, branch: str = "main") -> bool:
        """Push any JSON file to repo's main branch. Overwrites if exists."""
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}"
        print(f"Pushing {filename} to {owner}/{repo}:{branch}...")
        
        existing_sha = None
        try:
            check_resp, idx = self._request("GET", url, params={"ref": branch})
            self.token_pool.update_rate_limit(idx, check_resp)
            if check_resp.status_code == 200:
                existing_sha = check_resp.json().get("sha")
                print(f"  Updating existing file (SHA: {existing_sha[:7]}...)")
        except:
            pass
        
        content_json = json.dumps(data, indent=2, ensure_ascii=False)
        content_b64 = base64.b64encode(content_json.encode("utf-8")).decode()
        
        payload = {
            "message": f"Update {filename} via sync tool",
            "content": content_b64,
            "branch": branch
        }
        if existing_sha:
            payload["sha"] = existing_sha
        
        try:
            resp = self._put(url, payload)
            if resp.status_code in (200, 201):
                print(f"  ✓ Pushed successfully")
                return True
            else:
                print(f"  ✗ Push failed: {resp.status_code} {resp.text[:100]}")
                return False
        except Exception as e:
            print(f"  ✗ Push error: {e}")
            return False

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
            resp = self._get(
                f"https://api.github.com/repos/{owner}/{repo}/branches",
                {"per_page": 100, "page": page}
            )
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
            idx = self.token_pool.find_available_token()
            session = self.token_pool.get_session(idx)
            raw = session.get(data["download_url"], timeout=30)
            self.token_pool.update_rate_limit(idx, raw)
            raw.raise_for_status()
            return base64.b64encode(raw.content).decode()
        
        return None

    def get_default_branch_sha(self, owner: str, repo: str) -> str:
        resp = self._get(f"https://api.github.com/repos/{owner}/{repo}")
        default = resp.json().get("default_branch", "main")
        ref = self._get(f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{default}")
        return ref.json()["object"]["sha"]

    def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
        idx = self.token_pool.find_available_token()
        session = self.token_pool.get_session(idx)
        resp = session.get(url, timeout=10)
        self.token_pool.update_rate_limit(idx, resp)
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
        idx = self.token_pool.find_available_token()
        session = self.token_pool.get_session(idx)
        check = session.get(url, params={"ref": branch}, timeout=10)
        self.token_pool.update_rate_limit(idx, check)
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
        
        print(f"\n{'='*60}")
        print("STEP 1: Recover state from repo2 main branch")
        print(f"{'='*60}")
        
        # PULL missing_branches.json from repo2 main
        missing_data = self.pull_json_from_repo(tgt_owner, tgt_repo, self.MISSING_FILENAME, branch="main")
        
        if missing_data:
            missing = sorted(missing_data.get("missing_branches", []))
            print(f"  ✓ Recovered missing list from repo2: {len(missing):,} branches")
        else:
            # Fallback: local file or compute
            if missing_file and os.path.exists(missing_file):
                print(f"\nLoading local missing file: {missing_file}")
                with open(missing_file, "r") as f:
                    data = json.load(f)
                missing = sorted(data.get("missing_branches", []))
                print(f"  ✓ Loaded {len(missing)} branches from local file")
            else:
                print(f"\nComputing missing branches by comparing repos...")
                print(f"[Source] {source}")
                source_branches = self.get_branches(src_owner, src_repo)
                
                print(f"[Target] {target}")
                target_branches = self.get_branches(tgt_owner, tgt_repo)
                
                missing = sorted(source_branches - target_branches)
                print(f"  Source: {len(source_branches):,} | Target: {len(target_branches):,} | Missing: {len(missing):,}")
            
            # PUSH missing list to repo2 main
            if missing:
                missing_data = {
                    "source": source,
                    "target": target,
                    "missing_count": len(missing),
                    "missing_branches": missing,
                    "generated_at": datetime.utcnow().isoformat() + "Z"
                }
                self.push_json_to_repo(tgt_owner, tgt_repo, self.MISSING_FILENAME, missing_data, branch="main")
                # Save locally too
                local_path = os.path.join(self.output_dir, f"missing_branches_{src_owner}_{src_repo}_to_{tgt_owner}_{tgt_repo}.json")
                with open(local_path, "w") as f:
                    json.dump(missing_data, f, indent=2)
        
        # PULL progress from repo2 main
        print(f"\n{'='*60}")
        print("STEP 2: Recover progress from repo2 main branch")
        print(f"{'='*60}")
        
        repo_progress = self.pull_json_from_repo(tgt_owner, tgt_repo, self.PROGRESS_FILENAME, branch="main")
        local_progress = self.load_progress()
        
        if repo_progress:
            repo_completed = set(repo_progress.get("completed", []))
            repo_failed = repo_progress.get("failed", [])
            print(f"  Repo progress: {len(repo_completed)} completed, {len(repo_failed)} failed")
        else:
            repo_completed = set()
            repo_failed = []
            print(f"  No progress found in repo")
        
        local_completed = set(local_progress.get("completed", []))
        local_failed = local_progress.get("failed", [])
        print(f"  Local progress: {len(local_completed)} completed, {len(local_failed)} failed")
        
        # Merge: use whichever has MORE completed (repo wins if it's ahead)
        if len(repo_completed) >= len(local_completed):
            completed = repo_completed
            failed = repo_failed
            print(f"  ✓ Using repo progress (more complete)")
        else:
            completed = local_completed
            failed = local_failed
            print(f"  ✓ Using local progress (more complete)")
        
        if not missing:
            print("\n✓ Nothing to sync!")
            return
        
        if dry_run:
            print(f"\n[DRY RUN] Would sync {len(missing)} branches with {self.workers} workers")
            return
        
        print(f"\n{'='*60}")
        print("STEP 3: Sync branches")
        print(f"{'='*60}")
        
        print("Getting target base SHA...")
        tgt_base_sha = self.get_default_branch_sha(tgt_owner, tgt_repo)
        print(f"  Base: {tgt_base_sha[:7]}...\n")
        
        to_process = [b for b in missing if b not in completed]
        print(f"Resume: {len(completed)} done, {len(to_process)} remaining")
        print(f"Workers: {self.workers}")
        print(f"Tokens: {self.token_pool.token_count} (~{self.token_pool.token_count * 5000:,}/hour)\n")
        
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
                            progress_data = {
                                "completed": sorted(list(completed)), 
                                "failed": failed,
                                "updated_at": datetime.utcnow().isoformat() + "Z",
                                "total_target": len(missing)
                            }
                            self.save_progress(progress_data)
                            self.push_json_to_repo(tgt_owner, tgt_repo, self.PROGRESS_FILENAME, progress_data, branch="main")
                            print(f"  💾 Progress saved & pushed ({len(completed)}/{len(missing)} total)")
                            processed_since_save = 0
                        
        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted.")
        finally:
            progress_data = {
                "completed": sorted(list(completed)), 
                "failed": failed,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "total_target": len(missing)
            }
            self.save_progress(progress_data)
            self.push_json_to_repo(tgt_owner, tgt_repo, self.PROGRESS_FILENAME, progress_data, branch="main")
        
        print(f"\n{'='*60}")
        print("DONE")
        print(f"Total completed: {len(completed)}")
        print(f"Total failed:    {len(failed)}")
        print(f"This run:        +{completed_new} done, +{failed_new} failed")
        print(f"Progress file:   {self.PROGRESS_FILENAME} in repo2 main")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Multi-token Lua sync. Backs up missing list + progress to repo2 main branch."
    )
    parser.add_argument("source", help="Source repo (owner/repo)")
    parser.add_argument("target", help="Target repo (owner/repo)")
    parser.add_argument("--token", "-t", required=True, action="append", 
                        help="GitHub token (use multiple for more speed)")
    parser.add_argument("--missing-file", "-m", help="Local missing_branches JSON (fallback only)")
    parser.add_argument("--output-dir", "-o", default=".", help="Cache/progress directory")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Preview only")
    parser.add_argument("--cache-hours", "-c", type=int, default=24, help="Branch cache max age")
    parser.add_argument("--workers", "-w", type=int, default=50, help="Concurrent threads")
    
    args = parser.parse_args()
    
    if len(args.token) == 1:
        print("WARNING: Only 1 token. Rate limit: 5,000/hour.")
    else:
        print(f"Using {len(args.token)} tokens. Budget: ~{len(args.token) * 5000:,}/hour.")
    
    sync = LuaBranchSync(
        tokens=args.token, 
        output_dir=args.output_dir, 
        cache_hours=args.cache_hours,
        workers=args.workers
    )
    
    print("=" * 60)
    print("Multi-Token Lua Sync (Repo-Backed State)")
    print("=" * 60)
    print(f"Source:  {args.source}")
    print(f"Target:  {args.target}")
    print(f"Workers: {args.workers}")
    print(f"Tokens:  {len(args.token)}")
    print(f"{'='*60}")
    
    sync.run(args.source, args.target, missing_file=args.missing_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
