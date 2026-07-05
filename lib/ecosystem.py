"""Ecosystem discovery — search skills.sh for relevant skills."""

import subprocess
import json
import os


def suggest_skills(project=None, archetype=None):
    """Search skills.sh for skills relevant to a project's tech stack."""
    from registry import Registry
    reg = Registry()

    if project:
        archetype = reg.get_archetype(project)
        project_path = reg.get_path(project)
    else:
        project_path = os.getcwd()
        if not archetype:
            archetype = "tool"

    # Determine search terms from archetype + project contents
    search_terms = []
    archetype_terms = {
        "production": ["deployment", "docker", "monitoring"],
        "research": ["data-science", "jupyter", "pandas"],
        "frontend": ["react", "typescript", "testing"],
        "tool": ["cli", "python", "testing"],
        "infra": ["terraform", "ansible", "docker"],
    }
    search_terms.extend(archetype_terms.get(archetype, []))

    # Detect tech stack from project
    if os.path.isdir(project_path):
        files = os.listdir(project_path)
        if "Cargo.toml" in files:
            search_terms.append("rust")
        if "package.json" in files:
            search_terms.append("node")
        if "pyproject.toml" in files or "setup.py" in files:
            search_terms.append("python")
        if any(f.endswith(".tf") for f in files):
            search_terms.append("terraform")

    if not search_terms:
        print("No search terms determined.")
        return

    print(f"Searching skills.sh for: {', '.join(search_terms)}")
    print(f"Archetype: {archetype}")
    print()

    # Search using npx skills find
    for term in search_terms[:3]:  # limit to avoid rate limiting
        try:
            result = subprocess.run(
                ["npx", "skills", "find", term],
                capture_output=True, text=True,
                timeout=30
            )
            if result.stdout.strip():
                print(f"=== Results for '{term}' ===")
                # Show first 10 lines
                for line in result.stdout.strip().split('\n')[:10]:
                    print(f"  {line}")
                print()
        except subprocess.TimeoutExpired:
            print(f"  Search for '{term}' timed out")
        except FileNotFoundError:
            print("npx skills not available. Install with: npm install -g skills")
            return
