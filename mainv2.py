#!/usr/bin/env python3
"""
GitHub Branch Sync Tool - CACHE-FIRST VERSION
Reads saved branch lists from JSON first. Only hits the API if cache is missing or expired.
"""

import requests
import sys
import argparse
import json
import time
import os
from typing import Set, List, Dict, Optional
from datetime import datetime


class GitHubBranchSync:
    def __init__(self, token: Optional[str] = None, output_dir: str = ".", cache_max_age_hours: int = 24):
        self.base_url = "https://api.github.com"
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "BranchSyncTool/1.0"
        })
        if token:
            self.session.headers["Authorization"] = f"token {token}"
        
        self.rate_limit_remaining = 5000
        self.rate_limit_reset = 0
        self.output_dir = output_dir
        self.cache_max_age_hours = cache_max_age_hours
        os.makedirs(output_dir, exist_ok=True)

    def _get_cache_path(self, owner: str, repo: str) -> str:
        return os.path.join(self.output_dir, f"{owner}_{repo}_branches.json")

    def _save_json(self, filename: str, data: dict):
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  💾 Saved to {filepath}")
        return filepath

    def load_saved_branches(self, owner: str, repo: str) -> Optional[Set[str]]:
        """Load branches from saved JSON if it exists and isn't too old."""
        filepath = self._get_cache_path(owner, repo)
        
        if not os.path.exists(filepath):
            return None
        
        try:
            mtime = os.path.getmtime(filepath)
            age_hours = (time.time() - mtime) / 3600
            
            if age_hours > self.cache_max_age_hours:
                print(f"  ⚠ Cache file is {age_hours:.1f}h old (max {self.cache_max_age_hours}h), refetching...")
                return None
            
            print(f"  📂 Loading cached branches from {filepath}...")
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            branches = set(data.get("branches", []))
            print(f"  ✓ Loaded {len(branches)} branches from cache (age: {age_hours:.1f}h)")
            return branches
            
        except (json.JSONDecodeError, KeyError, OSError) as e:
            print(f"  ⚠ Failed to read cache: {e}, refetching...")
            return None

    def _check_rate_limit(self, response: requests.Response):
        self.rate_limit_remaining = int(response.headers.get('X-RateLimit-Remaining', 0))
        self.rate_limit_reset = int(response.headers.get('X-RateLimit-Reset', 0))
        
        if self.rate_limit_remaining < 10:
            sleep_time = max(self.rate_limit_reset - time.time(), 0) + 1
            print(f"Rate limit nearly exhausted. Sleeping for {sleep_time:.0f} seconds...")
            time.sleep(sleep_time)

    def _get(self, url: str, params: Dict = None) -> requests.Response:
        response = self.session.get(url, params=params or {}, timeout=30)
        self._check_rate_limit(response)
        
        if response.status_code == 403 and 'rate limit' in response.text.lower():
            retry_after = int(response.headers.get('Retry-After', 60))
            print(f"Rate limited. Waiting {retry_after} seconds...")
            time.sleep(retry_after)
            return self._get(url, params)
        
        response.raise_for_status()
        return response

    def get_all_branches(self, owner: str, repo: str, per_page: int = 100) -> Set[str]:
        branches = set()
        page = 1
        
        print(f"Fetching branches from {owner}/{repo} via API...")
        
        while True:
            url = f"{self.base_url}/repos/{owner}/{repo}/branches"
            params = {"per_page": per_page, "page": page}
            
            try:
                response = self._get(url, params)
                data = response.json()
                
                if not data:
                    break
                
                for branch in data:
                    branches.add(branch['name'])
                
                if len(data) < per_page:
                    break
                
                page += 1
                
                if page % 10 == 0:
                    print(f"  ... fetched {len(branches)} branches so far (page {page})")
                    
            except requests.exceptions.RequestException as e:
                print(f"Error fetching page {page}: {e}")
                raise
        
        print(f"Total branches fetched from API: {len(branches)}")
        
        self._save_json(
            f"{owner}_{repo}_branches.json",
            {
                "repository": f"{owner}/{repo}",
                "total_count": len(branches),
                "branches": sorted(list(branches)),
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "method": "rest_api"
            }
        )
        
        return branches

    def get_all_branches_graphql(self, owner: str, repo: str) -> Set[str]:
        if "Authorization" not in self.session.headers:
            print("No token provided, falling back to REST API...")
            return self.get_all_branches(owner, repo)
        
        branches = set()
        cursor = None
        
        query = """
        query($owner: String!, $repo: String!, $cursor: String) {
            repository(owner: $owner, name: $repo) {
                refs(refPrefix: "refs/heads/", first: 100, after: $cursor) {
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                    nodes {
                        name
                    }
                }
            }
        }
        """
        
        print(f"Fetching branches from {owner}/{repo} via GraphQL...")
        
        while True:
            variables = {
                "owner": owner,
                "repo": repo,
                "cursor": cursor
            }
            
            try:
                response = self.session.post(
                    "https://api.github.com/graphql",
                    json={"query": query, "variables": variables}
                )
                self._check_rate_limit(response)
                response.raise_for_status()
                
                data = response.json()
                
                if 'errors' in data:
                    print(f"GraphQL errors: {data['errors']}")
                    print("Falling back to REST API...")
                    return self.get_all_branches(owner, repo)
                
                refs = data['data']['repository']['refs']
                
                for node in refs['nodes']:
                    branches.add(node['name'])
                
                if not refs['pageInfo']['hasNextPage']:
                    break
                
                cursor = refs['pageInfo']['endCursor']
                
                if len(branches) % 1000 == 0:
                    print(f"  ... fetched {len(branches)} branches so far")
                    
            except requests.exceptions.RequestException as e:
                print(f"GraphQL error: {e}, falling back to REST API...")
                return self.get_all_branches(owner, repo)
        
        print(f"Total branches fetched via GraphQL: {len(branches)}")
        
        self._save_json(
            f"{owner}_{repo}_branches.json",
            {
                "repository": f"{owner}/{repo}",
                "total_count": len(branches),
                "branches": sorted(list(branches)),
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "method": "graphql"
            }
        )
        
        return branches

    def get_default_branch_sha(self, owner: str, repo: str) -> str:
        url = f"{self.base_url}/repos/{owner}/{repo}"
        response = self._get(url)
        default_branch = response.json().get('default_branch', 'main')
        
        ref_url = f"{self.base_url}/repos/{owner}/{repo}/git/ref/heads/{default_branch}"
        ref_response = self._get(ref_url)
        sha = ref_response.json()['object']['sha']
        
        print(f"Target default branch: {default_branch} @ {sha[:7]}...")
        return sha

    def create_branch(self, owner: str, repo: str, branch_name: str, base_sha: str) -> bool:
        url = f"{self.base_url}/repos/{owner}/{repo}/git/refs"
        payload = {
            "ref": f"refs/heads/{branch_name}",
            "sha": base_sha
        }
        
        try:
            response = self.session.post(url, json=payload)
            self._check_rate_limit(response)
            
            if response.status_code == 201:
                print(f"  ✓ Created branch: {branch_name}")
                return True
            elif response.status_code == 422 and 'already exists' in response.text:
                print(f"  ⚠ Branch already exists: {branch_name}")
                return True
            else:
                print(f"  ✗ Failed to create {branch_name}: {response.status_code} - {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"  ✗ Error creating {branch_name}: {e}")
            return False

    def get_branches(self, owner: str, repo: str, use_graphql: bool = False) -> Set[str]:
        """
        CACHE-FIRST: Try to load from JSON file first.
        Only hits the API if no valid cache exists.
        """
        cached = self.load_saved_branches(owner, repo)
        if cached is not None:
            return cached
        
        if use_graphql:
            return self.get_all_branches_graphql(owner, repo)
        else:
            return self.get_all_branches(owner, repo)

    def sync_branches(
        self,
        source_owner: str,
        source_repo: str,
        target_owner: str,
        target_repo: str,
        dry_run: bool = False,
        use_graphql: bool = False
    ) -> Dict[str, any]:
        stats = {
            "source_branches": 0,
            "target_branches": 0,
            "missing_branches": 0,
            "created": 0,
            "failed": 0,
            "skipped": 0
        }
        
        # CACHE-FIRST: Load from JSON if available, else fetch from API
        print(f"\n[Source] {source_owner}/{source_repo}")
        source_branches = self.get_branches(source_owner, source_repo, use_graphql=use_graphql)
        
        print(f"\n[Target] {target_owner}/{target_repo}")
        target_branches = self.get_branches(target_owner, target_repo, use_graphql=use_graphql)
        
        stats["source_branches"] = len(source_branches)
        stats["target_branches"] = len(target_branches)
        
        # Find missing branches
        missing = source_branches - target_branches
        stats["missing_branches"] = len(missing)
        
        if missing:
            self._save_json(
                f"missing_branches_{source_owner}_{source_repo}_to_{target_owner}_{target_repo}.json",
                {
                    "source": f"{source_owner}/{source_repo}",
                    "target": f"{target_owner}/{target_repo}",
                    "missing_count": len(missing),
                    "missing_branches": sorted(list(missing)),
                    "generated_at": datetime.utcnow().isoformat() + "Z"
                }
            )
        
        if not missing:
            print("\n✓ All branches are already in sync!")
            self._save_json(
                f"sync_result_{source_owner}_{source_repo}_to_{target_owner}_{target_repo}.json",
                {**stats, "status": "already_in_sync", "timestamp": datetime.utcnow().isoformat() + "Z"}
            )
            return stats
        
        print(f"\nFound {len(missing)} branches to sync from {source_owner}/{source_repo} to {target_owner}/{target_repo}")
        
        target_base_sha = self.get_default_branch_sha(target_owner, target_repo)
        
        if dry_run:
            print("\n[DRY RUN] Would create the following branches:")
            for branch in sorted(missing):
                print(f"  - {branch}")
            
            self._save_json(
                f"sync_result_{source_owner}_{source_repo}_to_{target_owner}_{target_repo}.json",
                {**stats, "status": "dry_run", "timestamp": datetime.utcnow().isoformat() + "Z"}
            )
            return stats
        
        print(f"\nSyncing branches (all pointing to {target_base_sha[:7]}...)")
        created_branches = []
        failed_branches = []
        
        for i, branch in enumerate(sorted(missing), 1):
            print(f"[{i}/{len(missing)}] Processing: {branch}")
            
            success = self.create_branch(target_owner, target_repo, branch, target_base_sha)
            if success:
                stats["created"] += 1
                created_branches.append(branch)
            else:
                stats["failed"] += 1
                failed_branches.append({"branch": branch, "reason": "api_error"})
            
            time.sleep(0.1)
        
        self._save_json(
            f"sync_result_{source_owner}_{source_repo}_to_{target_owner}_{target_repo}.json",
            {
                "source": f"{source_owner}/{source_repo}",
                "target": f"{target_owner}/{target_repo}",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "base_sha": target_base_sha,
                "stats": stats,
                "created_branches": created_branches,
                "failed_branches": failed_branches
            }
        )
        
        return stats


def parse_repo_string(repo_str: str) -> tuple:
    parts = repo_str.split('/')
    if len(parts) != 2:
        raise ValueError(f"Invalid repo format '{repo_str}'. Expected 'owner/repo'.")
    return parts[0], parts[1]


def main():
    parser = argparse.ArgumentParser(
        description="Sync missing branches from one GitHub repo to another. Reads cached JSON first, API only if needed."
    )
    parser.add_argument("source", help="Source repository (format: owner/repo)")
    parser.add_argument("target", help="Target repository (format: owner/repo)")
    parser.add_argument("--token", "-t", help="GitHub Personal Access Token")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Show what would be synced without making changes")
    parser.add_argument("--graphql", "-g", action="store_true", help="Use GraphQL API")
    parser.add_argument("--output-dir", "-o", default=".", help="Directory to save result files")
    parser.add_argument("--cache-hours", "-c", type=int, default=24, help="Max age of cache files in hours before refetching (default: 24)")
    
    args = parser.parse_args()
    
    source_owner, source_repo = parse_repo_string(args.source)
    target_owner, target_repo = parse_repo_string(args.target)
    
    sync = GitHubBranchSync(token=args.token, output_dir=args.output_dir, cache_max_age_hours=args.cache_hours)
    
    print("=" * 60)
    print("GitHub Branch Sync Tool (CACHE-FIRST)")
    print("=" * 60)
    print(f"Source: {source_owner}/{source_repo}")
    print(f"Target: {target_owner}/{target_repo}")
    print(f"Cache max age: {args.cache_hours} hours")
    print(f"Mode: {'GraphQL' if args.graphql else 'REST API'}")
    print(f"Dry Run: {'Yes' if args.dry_run else 'No'}")
    print(f"Output Dir: {args.output_dir}")
    if not args.token:
        print("Warning: No token provided. Rate limit is 60 requests/hour.")
    print("=" * 60)
    
    try:
        stats = sync.sync_branches(
            source_owner, source_repo,
            target_owner, target_repo,
            dry_run=args.dry_run,
            use_graphql=args.graphql
        )
        
        print("\n" + "=" * 60)
        print("SYNC SUMMARY")
        print("=" * 60)
        print(f"Source branches:      {stats['source_branches']:,}")
        print(f"Target branches:        {stats['target_branches']:,}")
        print(f"Missing branches:     {stats['missing_branches']:,}")
        if not args.dry_run:
            print(f"Created:              {stats['created']:,}")
            print(f"Failed:               {stats['failed']:,}")
            print(f"Skipped:              {stats['skipped']:,}")
        print("=" * 60)
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            print("\n✗ Error: Repository not found or not accessible.")
        elif e.response.status_code == 401:
            print("\n✗ Error: Authentication failed. Check your token.")
        else:
            print(f"\n✗ HTTP Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
