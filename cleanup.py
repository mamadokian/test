#!/usr/bin/env python3
"""
Cleanup script - deletes all branches from repo2 that were wrongly created.
Keeps only the default branch.
"""

import requests
import argparse
import json
import time
from typing import List


class BranchCleanup:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
            "User-Agent": "BranchCleanup/1.0"
        })
        self.base_url = "https://api.github.com"

    def get_default_branch(self, owner: str, repo: str) -> str:
        url = f"{self.base_url}/repos/{owner}/{repo}"
        response = self.session.get(url)
        response.raise_for_status()
        return response.json().get("default_branch", "main")

    def get_all_branches(self, owner: str, repo: str) -> List[str]:
        branches = []
        page = 1
        while True:
            url = f"{self.base_url}/repos/{owner}/{repo}/branches"
            params = {"per_page": 100, "page": page}
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            if not data:
                break
            for b in data:
                branches.append(b["name"])
            if len(data) < 100:
                break
            page += 1
        return branches

    def delete_branch(self, owner: str, repo: str, branch: str) -> bool:
        url = f"{self.base_url}/repos/{owner}/{repo}/git/refs/heads/{branch}"
        response = self.session.delete(url)
        if response.status_code == 204:
            print(f"  ✓ Deleted: {branch}")
            return True
        elif response.status_code == 422:
            print(f"  ⚠ Cannot delete (protected?): {branch}")
            return False
        else:
            print(f"  ✗ Failed to delete {branch}: {response.status_code}")
            return False

    def cleanup(self, owner: str, repo: str, keep_file: str = None):
        default_branch = self.get_default_branch(owner, repo)
        print(f"Keeping default branch: {default_branch}")

        # If you have a saved JSON of the wrongly created branches, use that
        if keep_file:
            with open(keep_file, "r") as f:
                data = json.load(f)
            to_delete = [b for b in data.get("missing_branches", []) if b != default_branch]
        else:
            # Otherwise delete everything except default
            all_branches = self.get_all_branches(owner, repo)
            to_delete = [b for b in all_branches if b != default_branch]

        print(f"\nDeleting {len(to_delete)} branches from {owner}/{repo}...")
        print("This will take a while. Rate limit: 5000 requests/hour.")
        print("Ctrl+C to abort.\n")

        deleted = 0
        failed = 0

        for i, branch in enumerate(to_delete, 1):
            if i % 100 == 0:
                print(f"Progress: {i}/{len(to_delete)}...")
            if self.delete_branch(owner, repo, branch):
                deleted += 1
            else:
                failed += 1
            time.sleep(0.1)  # Be polite to the API

        print(f"\nDone. Deleted: {deleted}, Failed: {failed}")


def main():
    parser = argparse.ArgumentParser(description="Delete all branches except default")
    parser.add_argument("repo", help="owner/repo to clean up")
    parser.add_argument("--token", "-t", required=True, help="GitHub token")
    parser.add_argument("--keep-file", "-k", help="JSON file with branch list to delete (from previous run)")
    args = parser.parse_args()

    owner, repo = args.repo.split("/")
    cleaner = BranchCleanup(token=args.token)
    cleaner.cleanup(owner, repo, keep_file=args.keep_file)


if __name__ == "__main__":
    main()
