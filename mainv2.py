#!/usr/bin/env python3
"""
GitHub Branch Sync Tool
Compares branches between two repos and syncs missing ones from repo1 to repo2.
Optimized for repositories with large numbers of branches (100k+).
Now saves results to files.
"""

import requests
import sys
import argparse
import json
import time
import os
from typing import Set, List, Dict, Optional
from urllib.parse import urljoin
from datetime import datetime


class GitHubBranchSync:
    def __init__(self, token: Optional[str] = None, output_dir: str = "."):
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
        os.makedirs(output_dir, exist_ok=True)

    def _save_json(self, filename: str, data: dict):
        """Save data to a JSON file in the output directory."""
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  💾 Saved to {filepath}")
        return filepath

    def _check_rate_limit(self, response: requests.Response):
        """Update rate limit tracking from response headers."""
        self.rate_limit_remaining = int(response.headers.get('X-RateLimit-Remaining', 0))
        self.rate_limit_reset = int(response.headers.get('X-RateLimit-Reset', 0))
        
        if self.rate_limit_remaining < 10:
            sleep_time = max(self.rate_limit_reset - time.time(), 0) + 1
            print(f"Rate limit nearly exhausted. Sleeping for {sleep_time:.0f} seconds...")
            time.sleep(sleep_time)

    def _get(self, url: str, params: Dict = None) -> requests.Response:
        """Make authenticated GET request with rate limit handling."""
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
        """
        Fetch all branch names from a repository using pagination.
        Returns a set of branch names for O(1) lookup performance.
        """
        branches = set()
        page = 1
        
        print(f"Fetching branches from {owner}/{repo}...")
        
        while True:
            url = f"{self.base_url}/repos/{owner}/{repo}/branches"
            params = {
                "per_page": per_page,
                "page": page
            }
            
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
                
                # Progress indicator for large repos
                if page % 10 == 0:
                    print(f"  ... fetched {len(branches)} branches so far (page {page})")
                    
            except requests.exceptions.RequestException as e:
                print(f"Error fetching page {page}: {e}")
                raise
        
        print(f"Total branches in {owner}/{repo}: {len(branches)}")
        
        # Save branches to file
        self._save_json(
            f"{owner}_{repo}_branches.json",
            {
                "repository": f"{owner}/{repo}",
                "total_count": len(branches),
                "branches": sorted(list(branches)),
                "fetched_at": datetime.utcnow().isoformat() + "Z"
            }
        )
        
        return branches

    def get_all_branches_graphql(self, owner: str, repo: str) -> Set[str]:
        """
        Alternative: Use GraphQL for potentially faster fetching with cursor pagination.
        More efficient for very large repositories (100k+ branches).
        Requires token with appropriate scopes.
        """
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
        
        print(f"Total branches in {owner}/{repo}: {len(branches)}")
        
        # Save branches to file
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

    def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        """Check if a specific branch exists (lightweight check)."""
        url = f"{self.base_url}/repos/{owner}/{repo}/git/ref/heads/{branch}"
        response = self.session.get(url)
        return response.status_code == 200

    def create_branch(self, owner: str, repo: str, branch_name: str, source_sha: str) -> bool:
        """
        Create a new branch in the target repository.
        Returns True if successful, False otherwise.
        """
        url = f"{self.base_url}/repos/{owner}/{repo}/git/refs"
        payload = {
            "ref": f"refs/heads/{branch_name}",
            "sha": source_sha
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

    def get_branch_sha(self, owner: str, repo: str, branch: str) -> Optional[str]:
        """Get the SHA of the latest commit on a branch."""
        url = f"{self.base_url}/repos/{owner}/{repo}/git/ref/heads/{branch}"
        try:
            response = self._get(url)
            return response.json()['object']['sha']
        except (requests.exceptions.RequestException, KeyError) as e:
            print(f"Error getting SHA for {branch}: {e}")
            return None

    def sync_branches(
        self,
        source_owner: str,
        source_repo: str,
        target_owner: str,
        target_repo: str,
        dry_run: bool = False,
        use_graphql: bool = False
    ) -> Dict[str, any]:
        """
        Main sync logic: find branches in source but not in target, create them in target.
        Returns statistics about the sync operation.
        """
        stats = {
            "source_branches": 0,
            "target_branches": 0,
            "missing_branches": 0,
            "created": 0,
            "failed": 0,
            "skipped": 0
        }
        
        # Fetch branches from both repos
        if use_graphql:
            source_branches = self.get_all_branches_graphql(source_owner, source_repo)
            target_branches = self.get_all_branches_graphql(target_owner, target_repo)
        else:
            source_branches = self.get_all_branches(source_owner, source_repo)
            target_branches = self.get_all_branches(target_owner, target_repo)
        
        stats["source_branches"] = len(source_branches)
        stats["target_branches"] = len(target_branches)
        
        # Find missing branches (O(1) set difference)
        missing = source_branches - target_branches
        stats["missing_branches"] = len(missing)
        
        # Save missing branches to file
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
        
        if dry_run:
            print("\n[DRY RUN] Would create the following branches:")
            for branch in sorted(missing):
                print(f"  - {branch}")
            
            self._save_json(
                f"sync_result_{source_owner}_{source_repo}_to_{target_owner}_{target_repo}.json",
                {**stats, "status": "dry_run", "timestamp": datetime.utcnow().isoformat() + "Z"}
            )
            return stats
        
        # Get default branch SHA from source to use as base for new branches
        default_branch = self._get_default_branch(source_owner, source_repo)
        print(f"Using default branch '{default_branch}' as reference for new branches")
        
        # Create missing branches in target
        print("\nSyncing branches...")
        created_branches = []
        failed_branches = []
        
        for i, branch in enumerate(sorted(missing), 1):
            print(f"[{i}/{len(missing)}] Processing: {branch}")
            
            # Get the actual SHA from the source branch (preserves branch state)
            source_sha = self.get_branch_sha(source_owner, source_repo, branch)
            
            if not source_sha:
                print(f"  ✗ Could not get SHA for source branch {branch}, skipping...")
                stats["skipped"] += 1
                failed_branches.append({"branch": branch, "reason": "sha_not_found"})
                continue
            
            # Create branch in target
            success = self.create_branch(target_owner, target_repo, branch, source_sha)
            if success:
                stats["created"] += 1
                created_branches.append(branch)
            else:
                stats["failed"] += 1
                failed_branches.append({"branch": branch, "reason": "api_error"})
            
            # Small delay to avoid hitting rate limits too hard
            time.sleep(0.1)
        
        # Save final results
        self._save_json(
            f"sync_result_{source_owner}_{source_repo}_to_{target_owner}_{target_repo}.json",
            {
                "source": f"{source_owner}/{source_repo}",
                "target": f"{target_owner}/{target_repo}",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "stats": stats,
                "created_branches": created_branches,
                "failed_branches": failed_branches
            }
        )
        
        return stats

    def _get_default_branch(self, owner: str, repo: str) -> str:
        """Get the default branch name of a repository."""
        url = f"{self.base_url}/repos/{owner}/{repo}"
        response = self._get(url)
        return response.json().get('default_branch', 'main')


def parse_repo_string(repo_str: str) -> tuple:
    """Parse 'owner/repo' format."""
    parts = repo_str.split('/')
    if len(parts) != 2:
        raise ValueError(f"Invalid repo format '{repo_str}'. Expected 'owner/repo'.")
    return parts[0], parts[1]


def main():
    parser = argparse.ArgumentParser(
        description="Sync missing branches from one GitHub repo to another."
    )
    parser.add_argument(
        "source",
        help="Source repository (format: owner/repo)"
    )
    parser.add_argument(
        "target",
        help="Target repository (format: owner/repo)"
    )
    parser.add_argument(
        "--token", "-t",
        help="GitHub Personal Access Token (required for private repos, recommended for public to avoid rate limits)"
    )
    parser.add_argument(
        "--dry-run", "-d",
        action="store_true",
        help="Show what would be synced without making changes"
    )
    parser.add_argument(
        "--graphql", "-g",
        action="store_true",
        help="Use GraphQL API (faster for very large repos, requires token)"
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="Number of branches per page (max 100, default 100)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=".",
        help="Directory to save result files (default: current directory)"
    )
    
    args = parser.parse_args()
    
    # Parse repo strings
    source_owner, source_repo = parse_repo_string(args.source)
    target_owner, target_repo = parse_repo_string(args.target)
    
    # Initialize sync tool
    sync = GitHubBranchSync(token=args.token, output_dir=args.output_dir)
    
    print("=" * 60)
    print("GitHub Branch Sync Tool")
    print("=" * 60)
    print(f"Source: {source_owner}/{source_repo}")
    print(f"Target: {target_owner}/{target_repo}")
    print(f"Mode: {'GraphQL' if args.graphql else 'REST API'}")
    print(f"Dry Run: {'Yes' if args.dry_run else 'No'}")
    print(f"Output Dir: {args.output_dir}")
    if not args.token:
        print("Warning: No token provided. Rate limit is 60 requests/hour for public repos.")
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
        print(f"Target branches:      {stats['target_branches']:,}")
        print(f"Missing branches:     {stats['missing_branches']:,}")
        if not args.dry_run:
            print(f"Created:              {stats['created']:,}")
            print(f"Failed:               {stats['failed']:,}")
            print(f"Skipped:              {stats['skipped']:,}")
        print("=" * 60)
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            print("\n✗ Error: Repository not found or not accessible.")
            print("  - Check that the repo names are correct")
            print("  - For private repos, provide a valid token")
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
