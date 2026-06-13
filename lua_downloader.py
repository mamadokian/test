#!/usr/bin/env python3
"""
Lua File Downloader
Reads missing_branches.json and downloads {branch}.lua from each branch into a folder.
"""

import requests
import json
import os
import sys
import argparse
import concurrent.futures
import time
from typing import List, Optional


class LuaDownloader:
    def __init__(self, token: Optional[str] = None, output_dir: str = "lua_files", workers: int = 50):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "LuaDownloader/1.0"
        })
        if token:
            self.session.headers["Authorization"] = f"token {token}"
        
        self.output_dir = output_dir
        self.workers = workers
        os.makedirs(output_dir, exist_ok=True)
        
        self.rate_limit_remaining = 5000

    def _sleep_for_rate_limit(self, response: requests.Response):
        self.rate_limit_remaining = int(response.headers.get('X-RateLimit-Remaining', 0))
        if self.rate_limit_remaining < 5:
            reset = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
            sleep = max(reset - time.time(), 0) + 2
            print(f"  ⏳ Rate limit low ({self.rate_limit_remaining}). Sleeping {sleep:.0f}s...")
            time.sleep(sleep)

    def _get(self, url: str, params: dict = None, stream: bool = False) -> requests.Response:
        while True:
            resp = self.session.get(url, params=params or {}, timeout=30, stream=stream)
            self._sleep_for_rate_limit(resp)
            
            if resp.status_code == 403 and 'rate limit' in resp.text.lower():
                reset = int(resp.headers.get('X-RateLimit-Reset', time.time() + 60))
                time.sleep(max(reset - time.time(), 0) + 1)
                continue
            if resp.status_code == 404:
                return resp
            
            resp.raise_for_status()
            return resp

    def load_missing_branches(self, filepath: str) -> List[str]:
        print(f"Loading {filepath}...")
        with open(filepath, "r") as f:
            data = json.load(f)
        branches = data.get("missing_branches", [])
        print(f"  ✓ Loaded {len(branches)} branches")
        return branches

    def download_lua(self, owner: str, repo: str, branch: str) -> bool:
        """
        Download {branch}.lua from the given branch and save to output_dir/{branch}.lua
        """
        filepath = os.path.join(self.output_dir, f"{branch}.lua")
        
        # Skip if already exists
        if os.path.exists(filepath):
            return True  # Already downloaded
        
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{branch}.lua"
        
        try:
            resp = self._get(url, {"ref": branch})
            
            if resp.status_code == 404:
                print(f"  ✗ {branch}.lua not found")
                return False
            
            data = resp.json()
            
            # Method 1: Content included in response (small files)
            if data.get("encoding") == "base64" and data.get("content"):
                import base64
                content = base64.b64decode(data["content"].replace("\n", ""))
                with open(filepath, "wb") as f:
                    f.write(content)
                print(f"  ✓ {branch}.lua ({len(content)} bytes)")
                return True
            
            # Method 2: Download raw file (large files)
            if data.get("download_url"):
                raw = self._get(data["download_url"], stream=True)
                with open(filepath, "wb") as f:
                    for chunk in raw.iter_content(chunk_size=8192):
                        f.write(chunk)
                size = os.path.getsize(filepath)
                print(f"  ✓ {branch}.lua ({size} bytes, raw)")
                return True
            
            print(f"  ✗ {branch}.lua: no content available")
            return False
            
        except Exception as e:
            print(f"  ✗ {branch}.lua: {e}")
            return False

    def run(self, owner: str, repo: str, missing_file: str):
        branches = self.load_missing_branches(missing_file)
        
        if not branches:
            print("No branches to download!")
            return
        
        # Check which already exist
        to_download = []
        already_have = 0
        for branch in branches:
            path = os.path.join(self.output_dir, f"{branch}.lua")
            if os.path.exists(path):
                already_have += 1
            else:
                to_download.append(branch)
        
        print(f"\n{'='*60}")
        print(f"Output folder: {self.output_dir}")
        print(f"Total branches:  {len(branches)}")
        print(f"Already have:    {already_have}")
        print(f"To download:     {len(to_download)}")
        print(f"Workers:         {self.workers}")
        print(f"{'='*60}\n")
        
        if not to_download:
            print("✓ All files already downloaded!")
            return
        
        downloaded = 0
        failed = 0
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_branch = {
                executor.submit(self.download_lua, owner, repo, branch): branch
                for branch in to_download
            }
            
            for future in concurrent.futures.as_completed(future_to_branch):
                branch = future_to_branch[future]
                try:
                    success = future.result()
                except Exception as e:
                    print(f"  ✗ {branch}.lua: Exception - {e}")
                    success = False
                
                if success:
                    downloaded += 1
                else:
                    failed += 1
        
        print(f"\n{'='*60}")
        print("DONE")
        print(f"Downloaded: {downloaded}")
        print(f"Failed:     {failed}")
        print(f"Total:      {downloaded + failed}")
        print(f"Folder:     {os.path.abspath(self.output_dir)}")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Download .lua files from branches listed in missing_branches.json"
    )
    parser.add_argument("repo", help="Source repo (owner/repo)")
    parser.add_argument("missing_file", help="Path to missing_branches.json")
    parser.add_argument("--token", "-t", help="GitHub token (recommended for large lists)")
    parser.add_argument("--output-dir", "-o", default="lua_files", help="Folder to save .lua files")
    parser.add_argument("--workers", "-w", type=int, default=50, help="Concurrent downloads")
    
    args = parser.parse_args()
    
    owner, repo = args.repo.split("/")
    
    downloader = LuaDownloader(
        token=args.token,
        output_dir=args.output_dir,
        workers=args.workers
    )
    
    print("=" * 60)
    print("Lua File Downloader")
    print("=" * 60)
    print(f"Repo:        {args.repo}")
    print(f"Input:       {args.missing_file}")
    print(f"Output:      {args.output_dir}")
    print(f"Workers:     {args.workers}")
    if not args.token:
        print("Warning: No token. Rate limit: 60 requests/hour.")
    print("=" * 60)
    
    downloader.run(owner, repo, args.missing_file)


if __name__ == "__main__":
    main()
