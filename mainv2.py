#!/usr/bin/env python3
"""
Single-File Branch Sync - with missing_branches JSON support
"""

import requests
import base64
import json
import time
import os
import sys
import argparse
from typing import Set, Optional
from datetime import datetime


class LuaBranchSync:
    def __init__(self, token: str, output_dir: str = ".", cache_hours: int = 24):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
            "User-Agent": "LuaSync/1.0"
        })
        self.output_dir = output_dir
        self.cache_hours = cache_hours
        os.makedirs(output_dir, exist_ok=True)
        
        self.progress_file = os.path.join(output_dir, "lua_sync_progress.json")
        self.rate_limit_remaining = 5000

    def _sleep_for_rate_limit(self, response: requests.Response):
        self.rate_limit_remaining = int(response.headers.get('X-RateLimit-Remaining', 0))
        if self.rate_limit_remaining < 5:
            reset = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
            sleep = max(reset - time.time(), 0) + 2
            print(f"  ⏳ Rate limit low ({self.rate_limit_remaining}). Sleeping {sleep:.0f}s...")
            time.sleep(sleep)

    def _get(self, url: str, params: dict = None) -> requests.Response:
        while True:
            resp = self.session.get(url, params=params or {}, timeout=30)
            self._sleep_for_rate_limit(resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                reset = int(resp.headers.get('X-RateLimit-Reset', time.time() + 60))
                time.sleep(max(reset - time.time(), 0) + 1)
                continue
            if resp.status_code == 404:
                return resp
            resp.raise_for_status()
            return resp

    def _post(self, url: str, json_data: dict) -> requests.Response:
        while True:
            resp = self.session.post(url, json=json_data, timeout=30)
            self._sleep_for_rate_limit(resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                reset = int(resp.headers.get('X-RateLimit-Reset', time.time() + 60))
                time.sleep(max(reset - time.time(), 0) + 1)
                continue
            return resp

    def _put(self, url: str, json_data: dict) -> requests.Response:
        while True:
            resp = self.session.put(url, json=json_data, timeout=30)
            self._sleep_for_rate_limit(resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                reset = int(resp.headers.get('X-RateLimit-Reset', time.time() + 60))
                time.sleep(max(reset - time.time(), 0) + 1)
                continue
            return resp

    def load_progress(self) -> dict:
        if os.path.exists(self.progress_file):
            with open(self.progress_file, "r") as f:
                return json.load(f)
        return {"completed": [], "failed": []}

    def save_progress(self, progress: dict):
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
        """Load missing branches directly from the saved JSON file."""
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
            print(f"    ⚠ File {path} not found on branch {branch}")
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
        resp = self.session.get(f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}")
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
        print(f"    ✗ Failed to create branch: {resp.status_code} {resp.text[:100]}")
        return False

    def upload_lua(self, owner: str, repo: str, branch: str, content_b64: str) -> bool:
        path = f"{branch}.lua"
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        
        existing_sha = None
        check = self.session.get(url, params={"ref": branch}, timeout=10)
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
        
        if resp.status_code in (200, 201):
            return True
        
        print(f"    ✗ Upload failed: {resp.status_code} {resp.text[:100]}")
        return False

    def sync_branch(self, src_owner: str, src_repo: str, tgt_owner: str, tgt_repo: str, 
                    branch: str, tgt_base_sha: str) -> bool:
        print(f"  📥 {branch}.lua")
        content = self.get_lua_content(src_owner, src_repo, branch)
        if content is None:
            return False
        
        print(f"  🌿 Ensuring branch {branch}")
        if not self.branch_exists(tgt_owner, tgt_repo, branch):
            if not self.create_branch(tgt_owner, tgt_repo, branch, tgt_base_sha):
                return False
        
        print(f"  📤 Uploading {branch}.lua")
        return self.upload_lua(tgt_owner, tgt_repo, branch, content)

    def run(self, source: str, target: str, missing_file: str = None, dry_run: bool = False):
        src_owner, src_repo = source.split("/")
        tgt_owner, tgt_repo = target.split("/")
        
        # Determine which branches to sync
        if missing_file:
            # Use the provided missing_branches JSON directly
            missing = sorted(self.load_missing_branches(missing_file))
            print(f"\n{'='*60}")
            print(f"Using provided missing_branches file")
            print(f"Branches to sync: {len(missing):,}")
            print(f"{'='*60}\n")
        else:
            # Compare repos via API/cache
            print(f"\n[Source] {source}")
            source_branches = self.get_branches(src_owner, src_repo)
            
            print(f"\n[Target] {target}")
            target_branches = self.get_branches(tgt_owner, tgt_repo)
            
            missing = sorted(source_branches - target_branches)
            print(f"\n{'='*60}")
            print(f"Source branches:  {len(source_branches):,}")
            print(f"Target branches:  {len(target_branches):,}")
            print(f"Missing:          {len(missing):,}")
            print(f"{'='*60}\n")
        
        if not missing:
            print("✓ Nothing to sync!")
            return
        
        if dry_run:
            print("[DRY RUN] Would sync:")
            for b in missing[:10]:
                print(f"  - {b}.lua")
            if len(missing) > 10:
                print(f"  ... and {len(missing)-10} more")
            return
        
        # Get target base SHA once
        print("Getting target base SHA...")
        tgt_base_sha = self.get_default_branch_sha(tgt_owner, tgt_repo)
        print(f"  Base: {tgt_base_sha[:7]}...\n")
        
        # Resume progress
        progress = self.load_progress()
        completed = set(progress.get("completed", []))
        failed = list(progress.get("failed", []))
        
        to_process = [b for b in missing if b not in completed]
        print(f"Resume: {len(completed)} done, {len(to_process)} remaining\n")
        
        try:
            for i, branch in enumerate(to_process, 1):
                print(f"[{i}/{len(to_process)}] {branch}")
                
                ok = self.sync_branch(src_owner, src_repo, tgt_owner, tgt_repo, 
                                      branch, tgt_base_sha)
                
                if ok:
                    completed.add(branch)
                    print(f"  ✓ Done")
                else:
                    failed.append(branch)
                    print(f"  ✗ Failed")
                
                if i % 5 == 0:
                    self.save_progress({"completed": sorted(list(completed)), "failed": failed})
                    print(f"  💾 Saved progress")
                
                print()
                
        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted. Saving progress...")
        finally:
            self.save_progress({"completed": sorted(list(completed)), "failed": failed})
        
        print(f"\n{'='*60}")
        print("DONE")
        print(f"Completed: {len(completed)}")
        print(f"Failed:    {len(failed)}")
        print(f"Progress:  {self.progress_file}")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync .lua files from source branches to target repo."
    )
    parser.add_argument("source", help="Source repo (owner/repo)")
    parser.add_argument("target", help="Target repo (owner/repo)")
    parser.add_argument("--token", "-t", required=True, help="GitHub token")
    parser.add_argument("--missing-file", "-m", help="Path to missing_branches JSON from previous run (skips repo comparison)")
    parser.add_argument("--output-dir", "-o", default=".", help="Cache/progress directory")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Preview only")
    parser.add_argument("--cache-hours", "-c", type=int, default=24, help="Branch cache max age")
    
    args = parser.parse_args()
    
    sync = LuaBranchSync(token=args.token, output_dir=args.output_dir, cache_hours=args.cache_hours)
    sync.run(args.source, args.target, missing_file=args.missing_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
