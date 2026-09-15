#!/usr/bin/env python3
"""
setup_autocommit.py - point this at any local folder and it will:

  1. git init it (if needed), create/attach a GitHub repo via `gh`
  2. install a background watcher (macOS LaunchAgent) that commits + pushes
     every change, building a fine-grained, timestamped work history
  3. optionally render that history into an evidence timelapse video (via
     repo_timelapse.py, bundled alongside this script) so the work's
     evolution can be watched commit-by-commit - useful as
     work-authenticity evidence.

Usage:
    python3 setup_autocommit.py                              # interactive wizard, asks for the folder too
    python3 setup_autocommit.py ~/Documents/Latex/MyEssay     # interactive wizard, folder pre-filled
    python3 setup_autocommit.py ~/Documents/Latex/MyEssay --video --yes    # non-interactive, no prompts
    python3 setup_autocommit.py ~/Documents/Latex/MyEssay --public --interval 600
    python3 setup_autocommit.py ~/Documents/Latex/MyEssay --no-push --no-agent

Omit the path (or pass -i/--interactive) to walk through setup step by step;
give a path plus flags for a scriptable, non-interactive run. Run with
--help for all flags. Re-running on the same folder is safe -
existing repo/remote/watcher/LaunchAgent are detected and left alone (or
replaced in place for the watcher/LaunchAgent so options can be changed).
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_GITIGNORE = [
    ".DS_Store",
    "__pycache__/",
    "*.pyc",
    ".venv/",
    "venv/",
    "node_modules/",
    ".idea/",
    ".vscode/",
    "*.egg-info/",
    "dist/",
    "build/",
    ".env",
    ".texpadtmp/",
    "*.aux", "*.log", "*.synctex.gz", "*.fls", "*.fdb_latexmk", "*.out", "*.toc",
]

TEXT_EXTS = {
    ".py", ".md", ".txt", ".tex", ".ipynb", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".yaml", ".yml", ".c", ".cpp", ".h", ".hpp", ".java", ".go",
    ".rs", ".rb", ".php", ".html", ".css", ".scss", ".sh", ".swift", ".kt",
    ".m", ".r", ".jl", ".sql", ".toml", ".ini", ".cfg",
}
EXCLUDE_DIR_PARTS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", "site-packages",
}


# ---------------------------------------------------------------- helpers

def run(cmd, cwd=None, check=True, capture=False):
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        stderr = result.stderr if capture else ""
        raise RuntimeError(f"`{' '.join(cmd)}` failed: {stderr.strip()}")
    return result


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def check_path_sane(path):
    s = str(path)
    if "'" in s or '"' in s:
        print(
            f"\nWarning: the resolved path contains a quote character - this usually means a "
            f"shell-quoted path (like '/foo bar') got pasted in literally and the quotes were "
            f"kept as part of the path instead of stripped:\n  {path}\n"
            f"This is almost certainly not the folder you meant."
        )
        return ask_yes_no("Continue with this exact path anyway?", default=False)
    return True


def strip_wrapping_quotes(s):
    # guards against pasting a shell-quoted path (e.g. '/foo bar') straight
    # into a plain input() prompt, where the shell never gets a chance to
    # strip the quotes itself
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def ask_text(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = strip_wrapping_quotes(input(f"{prompt}{suffix}: ").strip())
        if raw:
            return raw
        if default is not None:
            return default
        print("This can't be empty.")


def ask_yes_no(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def ask_int(prompt, default):
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Not a number - using {default}.")
        return default


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or "repo"


# ------------------------------------------------------------------ git

def is_icloud_path(path):
    # covers both "iCloud Drive" (.../Mobile Documents/com~apple~CloudDocs/...)
    # and per-app iCloud containers (.../Mobile Documents/iCloud~.../...)
    return "Mobile Documents" in path.parts


def external_gitdir_for(slug):
    return Path.home() / ".local" / "share" / "git-repos" / f"{slug}.git"


def ensure_git_repo(path, slug, assume_yes):
    gitpath = path / ".git"
    icloud = is_icloud_path(path)

    if not gitpath.exists():
        if icloud:
            gitdir = external_gitdir_for(slug)
            gitdir.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "init", "-b", "main", f"--separate-git-dir={gitdir}"], cwd=path)
            print(f"Folder is in iCloud Drive - externalized git metadata to {gitdir}")
            print("(iCloud can evict/corrupt an in-place .git directory; keeping it outside iCloud avoids that)")
        else:
            run(["git", "init", "-b", "main"], cwd=path)
            print(f"Initialized git repo in {path}")
        return True

    if icloud and gitpath.is_dir():
        gitdir = external_gitdir_for(slug)
        print(f"Note: this folder is in iCloud Drive and its .git directory is still in-place at {gitpath}.")
        print("iCloud can evict/corrupt .git internals over time - this has bitten a prior setup before.")
        if confirm(f"Move git metadata out to {gitdir} now (git init --separate-git-dir - safe, keeps all history)?", assume_yes):
            gitdir.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "init", f"--separate-git-dir={gitdir}"], cwd=path)
            print(f"Externalized git metadata -> {gitdir}")
        else:
            print("Leaving .git inside iCloud - watch for corruption or 'Unable to read current working directory' errors.")
    return False


def ensure_gitignore(path):
    gi = path / ".gitignore"
    entries = list(DEFAULT_GITIGNORE)
    if is_icloud_path(path):
        entries.append("*.icloud")  # iCloud's placeholder for not-yet-downloaded files
    existing = gi.read_text().splitlines() if gi.exists() else []
    missing = [l for l in entries if l not in existing]
    if not missing:
        return
    with gi.open("a") as f:
        if existing and existing[-1] != "":
            f.write("\n")
        f.write("\n".join(missing) + "\n")
    print(f"Updated .gitignore ({len(missing)} entries added)")


def has_commits(path):
    return run(["git", "rev-parse", "--verify", "HEAD"], cwd=path, check=False, capture=True).returncode == 0


def ensure_initial_commit(path):
    if has_commits(path):
        return False
    run(["git", "add", "-A"], cwd=path)
    status = run(["git", "status", "--porcelain"], cwd=path, capture=True).stdout
    if status.strip():
        run(["git", "commit", "-m", "Initial commit"], cwd=path)
    else:
        run(["git", "commit", "--allow-empty", "-m", "Initial commit"], cwd=path)
    print("Created initial commit")
    return True


def current_branch(path):
    out = run(["git", "branch", "--show-current"], cwd=path, capture=True).stdout.strip()
    return out or "main"


def existing_remote(path):
    result = run(["git", "remote", "get-url", "origin"], cwd=path, check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_remote(path, name, remote_url, private, assume_yes):
    existing = existing_remote(path)
    if existing:
        print(f"Remote 'origin' already set: {existing}")
        return existing

    if remote_url:
        run(["git", "remote", "add", "origin", remote_url], cwd=path)
        print(f"Added remote origin -> {remote_url}")
        return remote_url

    if shutil.which("gh") is None:
        print("No 'gh' CLI found and no --remote given - skipping GitHub remote.")
        print("Add one later with: git remote add origin <url>")
        return None

    if run(["gh", "auth", "status"], check=False, capture=True).returncode != 0:
        print("gh is installed but not authenticated (run `gh auth login`) - skipping remote.")
        return None

    visibility = "private" if private else "public"
    if not confirm(f"Create a new {visibility} GitHub repo '{name}' and push this folder to it?", assume_yes):
        print("Skipped GitHub repo creation.")
        return None

    run(["gh", "repo", "create", name, f"--{visibility}", "--source=.", "--remote=origin"], cwd=path)
    print(f"Created GitHub repo '{name}' ({visibility}) and set as origin")
    return name


def push_initial(path, branch, assume_yes):
    if not existing_remote(path):
        return
    run(["git", "push", "-u", "origin", branch], cwd=path, check=False)


# ------------------------------------------------------------- watcher

WATCHER_TEMPLATE = textwrap.dedent("""\
    #!/bin/bash
    # Auto-generated by setup_autocommit.py - watches {target} for changes
    # and commits + pushes them, building a fine-grained work history.

    REPO_DIR="{target}"
    COMMIT_INTERVAL={heartbeat}
    CHECK_INTERVAL={poll}
    last_commit_time=$(date +%s)

    log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"; }}

    commit_and_push() {{
        local trigger="$1"
        cd "$REPO_DIR" || return 1
        if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
            # macOS file-coordination (iCloud/Spotlight/editor autosave) can
            # transiently lock files mid-write - retry a few times before
            # giving up on this poll cycle.
            local attempt=1
            while ! (git add -A && git commit -m "Auto-commit: $trigger at $(date '+%Y-%m-%d %H:%M:%S')"); do
                if [ $attempt -ge 3 ]; then
                    log "Commit failed after $attempt attempts, will retry next cycle"
                    return 1
                fi
                log "Commit attempt $attempt failed (transient lock?), retrying in 2s..."
                attempt=$((attempt + 1))
                sleep 2
            done
            if git remote | grep -q origin; then
                git push origin HEAD 2>/dev/null && log "Pushed to GitHub" || log "Push failed"
            fi
            last_commit_time=$(date +%s)
            return 0
        fi
        return 1
    }}

    log "Auto-commit watcher started for: $REPO_DIR"

    while true; do
        current_time=$(date +%s)
        time_since_commit=$((current_time - last_commit_time))

        cd "$REPO_DIR" || {{ sleep $CHECK_INTERVAL; continue; }}

        if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
            log "Changes detected, committing..."
            commit_and_push "changes detected"
        elif [ $time_since_commit -ge $COMMIT_INTERVAL ]; then
            log "heartbeat (no changes)"
            last_commit_time=$(date +%s)
        fi

        sleep $CHECK_INTERVAL
    done
    """)

PLIST_TEMPLATE = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
        <key>Label</key>
        <string>{label}</string>
        <key>ProgramArguments</key>
        <array>
            <string>/bin/bash</string>
            <string>{script_path}</string>
        </array>
        <key>EnvironmentVariables</key>
        <dict>
            <key>PATH</key>
            <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        </dict>
        <key>RunAtLoad</key>
        <true/>
        <key>KeepAlive</key>
        <true/>
        <key>StandardOutPath</key>
        <string>{log_path}</string>
        <key>StandardErrorPath</key>
        <string>{log_path}</string>
    </dict>
    </plist>
    """)


def install_watcher(path, slug, heartbeat, poll, assume_yes, no_agent):
    script_path = Path.home() / ".local" / "bin" / f"{slug}-autocommit.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(WATCHER_TEMPLATE.format(target=path, heartbeat=heartbeat, poll=poll))
    script_path.chmod(0o755)
    print(f"Wrote watcher script -> {script_path}")

    if no_agent:
        print(f"--no-agent given: run the watcher manually with:\n  {script_path}")
        return

    if sys.platform != "darwin":
        print(f"Not on macOS - skipping LaunchAgent. Run the watcher manually, or wrap it in your own systemd unit:\n  {script_path}")
        return

    label = f"com.max.{slug}-autocommit"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    log_path = f"/tmp/{slug}-autocommit.log"

    if is_icloud_path(path):
        print(
            "Note: this folder is in iCloud Drive. LaunchAgents can't read "
            "~/Library/Mobile Documents/ unless /bin/bash has Full Disk Access "
            "(System Settings -> Privacy & Security -> Full Disk Access). If the "
            f"log at {log_path} shows 'Unable to read current working directory: "
            "Operation not permitted', that's the fix."
        )

    if not confirm(
        f"Install + start a background LaunchAgent ({label}) that watches this folder "
        f"indefinitely and auto-pushes to GitHub?", assume_yes
    ):
        print(f"Skipped LaunchAgent install. Watcher script is still at:\n  {script_path}")
        return

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    plist_path.write_text(PLIST_TEMPLATE.format(label=label, script_path=script_path, log_path=log_path))
    run(["launchctl", "load", "-w", str(plist_path)])
    print(f"Installed and started LaunchAgent -> {plist_path}")
    print(f"Logs:         {log_path}")
    print(f"Stop with:    launchctl unload {plist_path}")
    print(f"Restart with: launchctl kickstart -k gui/$(id -u)/{label}")


# --------------------------------------------------------------- video

def auto_select_files(repo_path, max_files):
    counts = Counter(
        l for l in run(["git", "log", "--pretty=format:", "--name-only"], cwd=repo_path, capture=True)
        .stdout.splitlines() if l.strip()
    )
    head_files = set(run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=repo_path, capture=True).stdout.splitlines())
    candidates = []
    for f, _ in counts.most_common():
        if f not in head_files:
            continue
        parts = Path(f).parts
        if any(p in EXCLUDE_DIR_PARTS for p in parts):
            continue
        if Path(f).suffix.lower() not in TEXT_EXTS:
            continue
        candidates.append(f)
        if len(candidates) >= max_files:
            break
    return candidates


def resolve_file_patterns(repo_path, patterns):
    import fnmatch
    head_files = run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=repo_path, capture=True).stdout.splitlines()
    matched = sorted({f for f in head_files if any(fnmatch.fnmatch(f, pat) for pat in patterns)})
    return matched


def make_evidence_video(repo_path, output, files, max_commits, fps, seconds_per_commit):
    try:
        import repo_timelapse as rt
    except ImportError as e:
        raise RuntimeError(
            f"Video rendering needs repo_timelapse.py's dependencies (missing: {e}). "
            f"Install with: pip install -r {SCRIPT_DIR / 'requirements.txt'}"
        )

    rt.check_ffmpeg()
    work_root = Path(tempfile.mkdtemp(prefix="evidence_video_"))
    frame_dir = work_root / "frames"
    frame_dir.mkdir(parents=True)
    try:
        commits = rt.collect_commits(repo_path, files, since=None, until=None, max_commits=max_commits)
        if not commits:
            raise RuntimeError("No commits found for the tracked files.")

        font = rt.get_font(16)
        header_font = rt.get_font(18)
        style = rt.build_style_map("monokai") if rt.HAVE_PYGMENTS else None
        cfg = {"width": 1920, "height": 1080}
        file_states = {f: {"prev_lines": None, "scroll": 0} for f in files}

        concat_entries = []
        rendered = skipped = 0
        for i, commit in enumerate(commits, 1):
            print(f"\rRendering commit {i}/{len(commits)} (rendered {rendered})...", end="", flush=True)
            file_contents = {f: rt.get_file_content_at_commit(repo_path, commit["hash"], f) for f in files}
            if all(v is None for v in file_contents.values()):
                skipped += 1
                continue
            img = rt.render_frame(commit, file_contents, cfg, font, header_font, style, file_states)
            rendered += 1
            fname = f"frame_{rendered:05d}.png"
            img.save(frame_dir / fname)
            concat_entries.append((fname, seconds_per_commit))
        print()
        if not concat_entries:
            raise RuntimeError("Nothing to render - none of the tracked files had content across the selected commits.")

        print(f"Encoding video -> {output}")
        rt.encode_video(frame_dir, concat_entries, output, fps)
        print(f"Evidence video written: {output}")
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


# ------------------------------------------------------------ interactive

def interactive_wizard(prefill_path=None):
    from types import SimpleNamespace

    print("=== Auto-commit + evidence-video setup ===")
    print("(answer the questions below; press Enter to accept the default)\n")

    raw_path = ask_text("Folder to track", default=prefill_path or str(Path.cwd()))
    path = Path(raw_path).expanduser().resolve()
    if not check_path_sane(path):
        print("Aborting.")
        sys.exit(1)
    if not path.is_dir():
        if ask_yes_no(f"{path} doesn't exist yet - create it?", default=True):
            path.mkdir(parents=True, exist_ok=True)
        else:
            print("Aborting.")
            sys.exit(1)

    name = ask_text("GitHub repo name", default=path.name)

    no_push = not ask_yes_no("Push commits to GitHub?", default=True)
    remote = None
    public = False
    if not no_push and not existing_remote(path):
        if ask_yes_no("Do you already have a GitHub remote URL to use?", default=False):
            remote = ask_text("Remote URL")
        else:
            public = ask_yes_no("Make the new GitHub repo public? (default is private)", default=False)

    print()
    customize = ask_yes_no("Customize check timing? (defaults: check every 10s, heartbeat log every 5min)", default=False)
    poll = ask_int("Filesystem poll interval in seconds", 10) if customize else 10
    interval = ask_int("Heartbeat log interval in seconds", 300) if customize else 300

    no_agent = not ask_yes_no(
        "\nInstall a background LaunchAgent so this keeps running automatically forever?", default=True
    )

    print()
    do_video = ask_yes_no("Also render a commit-history evidence video?", default=False)
    files = None
    max_files = 6
    if do_video:
        if ask_yes_no("Auto-pick the most-edited files to show? (no = choose your own patterns)", default=True):
            max_files = ask_int("Max file panels", 6)
        else:
            raw = ask_text("File glob patterns, space-separated (e.g. *.md *.py)")
            files = raw.split()

    if no_push:
        push_desc = "no"
    elif remote:
        push_desc = remote
    else:
        push_desc = f"new {'public' if public else 'private'} repo via gh"

    video_desc = "no"
    if do_video:
        video_desc = " ".join(files) if files else f"auto-pick up to {max_files} files"

    print("\n--- summary ---")
    print(f"  Folder:       {path}")
    print(f"  Repo name:    {name}")
    print(f"  GitHub push:  {push_desc}")
    print(f"  Watcher:      poll {poll}s / heartbeat {interval}s")
    print(f"  LaunchAgent:  {'no' if no_agent else 'yes'}")
    print(f"  Video:        {video_desc}")
    if not ask_yes_no("\nProceed?", default=True):
        print("Aborting.")
        sys.exit(1)
    print()

    return SimpleNamespace(
        path=str(path), name=name, remote=remote, public=public, no_push=no_push,
        interval=interval, poll=poll, no_agent=no_agent, yes=True,
        video=do_video, files=files, max_files=max_files, video_output=None,
        fps=30, seconds_per_commit=0.4, max_commits=2000,
    )


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", default=None, help="local folder to track (omit for an interactive wizard)")
    parser.add_argument("-i", "--interactive", action="store_true", help="run the interactive setup wizard even if a path is given")
    parser.add_argument("--name", help="GitHub repo name (default: folder name)")
    parser.add_argument("--remote", help="use this existing remote URL instead of creating one via `gh`")
    parser.add_argument("--public", action="store_true", help="create the GitHub repo as public (default: private)")
    parser.add_argument("--no-push", action="store_true", help="skip GitHub entirely - local commit history only")
    parser.add_argument("--interval", type=int, default=300, help="heartbeat log interval in seconds (default 300)")
    parser.add_argument("--poll", type=int, default=10, help="filesystem poll interval in seconds (default 10)")
    parser.add_argument("--no-agent", action="store_true", help="write the watcher script but don't install a LaunchAgent")
    parser.add_argument("-y", "--yes", action="store_true", help="don't prompt for confirmation")

    video = parser.add_argument_group("evidence video")
    video.add_argument("--video", action="store_true", help="also render a commit-by-commit evidence timelapse")
    video.add_argument("--files", nargs="+", help="glob patterns of files to show (default: auto-pick the most-edited text files)")
    video.add_argument("--max-files", type=int, default=6, help="max file panels in the video (default 6)")
    video.add_argument("--video-output", help="output path (default: <folder>/work-evidence-timelapse.mp4)")
    video.add_argument("--fps", type=int, default=30)
    video.add_argument("--seconds-per-commit", type=float, default=0.4)
    video.add_argument("--max-commits", type=int, default=2000)

    args = parser.parse_args()

    if args.interactive or args.path is None:
        args = interactive_wizard(prefill_path=args.path)

    path = Path(args.path).expanduser().resolve()
    if not check_path_sane(path):
        parser.error("aborted")
    if not path.is_dir():
        parser.error(f"{path} is not a directory")

    name = args.name or path.name
    slug = slugify(name)

    ensure_git_repo(path, slug, args.yes)
    ensure_gitignore(path)
    ensure_initial_commit(path)
    branch = current_branch(path)

    if not args.no_push:
        ensure_remote(path, name, args.remote, private=not args.public, assume_yes=args.yes)
        push_initial(path, branch, args.yes)

    install_watcher(path, slug, args.interval, args.poll, args.yes, args.no_agent)

    if args.video:
        files = resolve_file_patterns(path, args.files) if args.files else auto_select_files(path, args.max_files)
        if not files:
            print("No files matched for the evidence video - skipping.")
        else:
            print(f"Evidence video will track: {', '.join(files)}")
            output = Path(args.video_output).expanduser() if args.video_output else path / "work-evidence-timelapse.mp4"
            make_evidence_video(path, output, files, args.max_commits, args.fps, args.seconds_per_commit)

    print(f"\nDone. {path} is now auto-committing on change.")


if __name__ == "__main__":
    main()
