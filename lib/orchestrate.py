"""Multi-agent orchestration — dispatch tasks to worktree-isolated agents."""

import json
import os
import re
import subprocess
import time
from datetime import datetime

DATA_DIR = os.environ.get("CLANKER_DATA", "/data/clanker")


def parse_plan(plan_path):
    """Parse a markdown plan into executable tasks.

    Expected format:
    ### Task N: Title
    **Files:** ...
    **Step 1:** ...
    ```code```
    """
    with open(plan_path) as f:
        content = f.read()

    tasks = []
    # Split by ### Task headers
    task_pattern = re.compile(r'### Task (\d+):\s*(.+?)(?=\n### Task |\Z)', re.DOTALL)
    for match in task_pattern.finditer(content):
        task_num = int(match.group(1))
        task_content = match.group(2).strip()

        # Extract title (first line)
        title_line = task_content.split('\n')[0].strip()

        # Extract files
        files_match = re.search(r'\*\*Files:\*\*\s*\n((?:- .+\n)*)', task_content)
        files = []
        if files_match:
            for line in files_match.group(1).strip().split('\n'):
                line = line.strip().lstrip('- ')
                if ':' in line:
                    files.append(line.split(':')[0].strip())

        # Extract code blocks
        code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', task_content, re.DOTALL)

        # Extract verification commands (lines starting with "Run:")
        verifications = re.findall(r'Run:\s*`([^`]+)`', task_content)

        tasks.append({
            "number": task_num,
            "title": title_line,
            "files": files,
            "code_blocks": code_blocks,
            "verifications": verifications,
            "raw": task_content[:500],
        })

    return tasks


def dispatch_task(task, project_path, worktree=False):
    """Dispatch a single task to an agent.

    If worktree=True, creates a git worktree for isolation.
    Returns the task result.
    """
    result = {
        "task": task["number"],
        "title": task["title"],
        "status": "pending",
        "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": None,
        "output": "",
        "errors": [],
    }

    work_dir = project_path

    if worktree:
        # Create worktree for isolation
        worktree_name = f"clanker-task-{task['number']}"
        worktree_path = os.path.join(project_path, ".clanker-worktrees", worktree_name)
        try:
            os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
            subprocess.run(
                ["git", "-C", project_path, "worktree", "add", worktree_path, "HEAD"],
                capture_output=True, text=True, check=True
            )
            work_dir = worktree_path
            result["worktree"] = worktree_path
        except subprocess.CalledProcessError as e:
            result["status"] = "failed"
            result["errors"].append(f"Worktree creation failed: {e.stderr}")
            return result

    # Execute code blocks
    for i, code in enumerate(task.get("code_blocks", [])):
        try:
            proc = subprocess.run(
                ["bash", "-c", code],
                cwd=work_dir,
                capture_output=True, text=True,
                timeout=300  # 5 min per block
            )
            result["output"] += f"--- Block {i+1} ---\n{proc.stdout}\n"
            if proc.returncode != 0:
                result["errors"].append(f"Block {i+1} failed (exit {proc.returncode}): {proc.stderr[:200]}")
        except subprocess.TimeoutExpired:
            result["errors"].append(f"Block {i+1} timed out (5 min)")

    # Run verifications
    for v in task.get("verifications", []):
        try:
            proc = subprocess.run(
                ["bash", "-c", v],
                cwd=work_dir,
                capture_output=True, text=True,
                timeout=60
            )
            if proc.returncode != 0:
                result["errors"].append(f"Verification failed: {v}")
        except:
            result["errors"].append(f"Verification error: {v}")

    # Clean up worktree
    if worktree and "worktree" in result:
        try:
            subprocess.run(
                ["git", "-C", project_path, "worktree", "remove", result["worktree"], "--force"],
                capture_output=True, text=True
            )
        except:
            pass

    result["status"] = "failed" if result["errors"] else "completed"
    result["completed_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return result


def orchestrate(plan_path, project_path=None, checkpoint_interval=3):
    """Orchestrate a plan — parse, dispatch tasks, checkpoint.

    checkpoint_interval: pause for human review every N tasks.
    """
    if not os.path.exists(plan_path):
        print(f"Plan not found: {plan_path}")
        return

    tasks = parse_plan(plan_path)
    if not tasks:
        print("No tasks found in plan.")
        return

    print(f"Parsed {len(tasks)} tasks from {plan_path}")
    print()

    if not project_path:
        project_path = os.getcwd()

    results = []
    for i, task in enumerate(tasks):
        print(f"=== Task {task['number']}: {task['title']} ===")
        print(f"  Files: {', '.join(task['files'][:5])}")
        print(f"  Code blocks: {len(task['code_blocks'])}")
        print(f"  Verifications: {len(task['verifications'])}")

        # Checkpoint
        if i > 0 and i % checkpoint_interval == 0:
            print(f"\n--- CHECKPOINT after {i} tasks ---")
            print(f"Completed: {sum(1 for r in results if r['status'] == 'completed')}")
            print(f"Failed: {sum(1 for r in results if r['status'] == 'failed')}")
            if not os.isatty(0):
                print("Non-interactive — continuing...")
            else:
                choice = input("Continue? [y/n/s(kip)] ").strip().lower()
                if choice == 'n':
                    print("Stopped by user.")
                    break
                elif choice == 's':
                    print(f"Skipping task {task['number']}")
                    results.append({"task": task["number"], "status": "skipped"})
                    continue

        # Check for contract violations before dispatching
        from contracts import scan_contracts
        contracts = scan_contracts(project_path)
        contract_count = sum(len(v) for v in contracts.values())
        if contract_count > 0 and task.get("files"):
            # Check if any task files are contract-defining files
            contract_files = set()
            for ctype, items in contracts.items():
                for item in items:
                    contract_files.add(item.get("file", ""))
            overlapping = set(task["files"]) & contract_files
            if overlapping:
                print(f"  ⚠ This task touches contract-defining files: {', '.join(overlapping)}")
                print(f"    {contract_count} contracts may be affected. Verify downstream consumers.")

        result = dispatch_task(task, project_path)
        results.append(result)

        if result["errors"]:
            print(f"  ERRORS:")
            for e in result["errors"]:
                print(f"    - {e}")
        else:
            print(f"  OK")
        print()

    # Summary
    print("=== Orchestration Summary ===")
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    print(f"Total: {len(results)} | Completed: {completed} | Failed: {failed} | Skipped: {skipped}")

    # Save results
    results_path = os.path.join(DATA_DIR, "reports", f"orchestration-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(results_path), exist_ok=True)
    with open(results_path, "w") as f:
        json.dump({"plan": plan_path, "results": results}, f, indent=2)
    print(f"Results saved: {results_path}")

    return results
