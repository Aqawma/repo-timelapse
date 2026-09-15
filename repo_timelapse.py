#!/usr/bin/env python3
"""
repo_timelapse.py - Render a timestamped timelapse video of how specific
files in a GitHub repo evolved over its commit history.

Usage:
    python3 repo_timelapse.py --config config.yaml

See example_config.yaml for the config format.
"""

import argparse
import datetime
import difflib
import fnmatch
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

try:
    from pygments import lex
    from pygments.lexers import get_lexer_for_filename, TextLexer
    from pygments.styles import get_style_by_name
    from pygments.util import ClassNotFound
    HAVE_PYGMENTS = True
except ImportError:
    HAVE_PYGMENTS = False


DEFAULTS = {
    "branch": None,
    "output": "timelapse.mp4",
    "fps": 30,
    "seconds_per_commit": 0.4,
    "max_commits": 300,
    "since": None,
    "until": None,
    "width": 1920,
    "height": 1080,
    "font_size": 16,
    "theme": "monokai",
    "keep_frames": False,
    "work_dir": None,
}


# ---------------------------------------------------------------- git utils

def run_git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def clone_or_open_repo(repo, work_dir, branch):
    path = Path(repo).expanduser()
    if path.is_dir() and (path / ".git").exists():
        print(f"Using existing local repo at {path}")
        return path

    dest = work_dir / "repo"
    print(f"Cloning {repo} -> {dest} (full history, this may take a while)...")
    args = ["clone", repo, str(dest)]
    if branch:
        args = ["clone", "-b", branch, repo, str(dest)]
    run_git(args, cwd=work_dir)
    return dest


def resolve_files(repo_path, patterns):
    all_files = run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=repo_path).splitlines()
    matched = sorted({
        f for f in all_files
        if any(fnmatch.fnmatch(f, pat) for pat in patterns)
    })
    if not matched:
        raise RuntimeError(
            f"No files at HEAD matched any of the configured patterns: {patterns}"
        )
    print(f"Tracking {len(matched)} file(s): {', '.join(matched)}")
    return matched


def collect_commits(repo_path, files, since, until, max_commits):
    seen = {}
    for f in files:
        args = ["log", "--follow", "--pretty=format:%H|%ct|%s"]
        if since:
            args += [f"--since={since}"]
        if until:
            args += [f"--until={until}"]
        args += ["--", f]
        out = run_git(args, cwd=repo_path)
        for line in out.splitlines():
            h, ts, subject = line.split("|", 2)
            seen[h] = (int(ts), subject)

    commits = [{"hash": h, "ts": ts, "subject": subject} for h, (ts, subject) in seen.items()]
    commits.sort(key=lambda c: c["ts"])

    if max_commits and len(commits) > max_commits:
        step = len(commits) / max_commits
        indices = sorted({int(i * step) for i in range(max_commits)})
        # always keep the very last commit so the timelapse ends at HEAD
        if indices[-1] != len(commits) - 1:
            indices[-1] = len(commits) - 1
        commits = [commits[i] for i in indices]

    print(f"{len(commits)} commit(s) will be rendered.")
    return commits


def get_file_content_at_commit(repo_path, commit_hash, filepath):
    result = subprocess.run(
        ["git", "show", f"{commit_hash}:{filepath}"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None  # file didn't exist yet at this commit
    return result.stdout


# ------------------------------------------------------------- rendering

def get_font(size, bold=False):
    candidates = [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.ttf",
        "/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def build_style_map(theme):
    if not HAVE_PYGMENTS:
        return None
    try:
        style = get_style_by_name(theme)
    except Exception:
        style = get_style_by_name("monokai")
    return style


def get_lexer(filepath, content):
    if not HAVE_PYGMENTS:
        return None
    try:
        return get_lexer_for_filename(filepath, content)
    except ClassNotFound:
        return TextLexer()


def token_color(style, token_type, default="#d4d4d4"):
    try:
        s = style.style_for_token(token_type)
        if s["color"]:
            return f"#{s['color']}"
    except Exception:
        pass
    return default


def compute_changed_range(prev_lines, lines):
    """Return (first, last) inclusive line indices in `lines` that differ from
    `prev_lines`, or None if there's no prior version to diff against, or if
    the two are identical."""
    if prev_lines is None or prev_lines == lines:
        return None
    sm = difflib.SequenceMatcher(a=prev_lines, b=lines, autojunk=False)
    first, last = None, None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if first is None:
            first = j1
        last = max(last or 0, j2 - 1, j1)
    if first is None:
        return None
    return (first, max(first, last))


def draw_line_clipped(draw, tokens, x0, y, max_width, font, fg_default):
    """Draw (text, color) pairs left-to-right on one row, clipping (not
    wrapping) once max_width is exceeded."""
    x = x0
    bbox = font.getbbox("m")
    char_w = max(bbox[2] - bbox[0], 1)
    for text, color in tokens:
        avail_px = max_width - (x - x0)
        if avail_px <= 0:
            break
        fit_chars = max(0, avail_px // char_w)
        if fit_chars == 0:
            break
        segment = text[:fit_chars]
        if not segment:
            continue
        draw.text((x, y), segment, font=font, fill=color or fg_default)
        seg_bbox = draw.textbbox((x, y), segment, font=font)
        x = seg_bbox[2]


def render_panel(draw, filepath, content, box, font, style, theme_bg, theme_fg, state):
    x0, y0, x1, y1 = box
    label_h = font.size + 8
    body_top = y0 + label_h + 4
    line_height = font.size + 4
    max_lines = max(1, (y1 - body_top) // line_height)
    max_width = (x1 - x0) - 12

    lines = content.splitlines() if content is not None else None
    prev_lines = state.get("prev_lines")
    changed_range = compute_changed_range(prev_lines, lines) if lines is not None else None
    is_new_file = lines is not None and prev_lines is None

    border = "#444444"
    if changed_range is not None or is_new_file:
        border = "#e8a33d"  # this file changed in this commit -> highlight the panel

    draw.rectangle(box, fill=theme_bg, outline=border, width=2)
    draw.rectangle((x0, y0, x1, y0 + label_h), fill="#2d2d2d")
    draw.text((x0 + 6, y0 + 4), filepath, font=font, fill="#ffffff")

    if lines is None:
        draw.text((x0 + 6, body_top), "(file does not exist yet)", font=font, fill="#777777")
        state["prev_lines"] = None
        state["scroll"] = 0
        return

    if not lines:
        state["prev_lines"] = lines
        state["scroll"] = 0
        return

    # Decide which window of lines to show: follow the edit, don't just show line 1.
    if changed_range is not None:
        first, last = changed_range
        span = last - first + 1
        if span >= max_lines:
            scroll = first
        else:
            scroll = max(0, first - (max_lines - span) // 2)
    else:
        scroll = state.get("scroll", 0)
    scroll = max(0, min(scroll, max(0, len(lines) - max_lines)))

    state["prev_lines"] = lines
    state["scroll"] = scroll

    lexer = get_lexer(filepath, content) if style is not None else None
    visible = lines[scroll: scroll + max_lines]
    for row, line_text in enumerate(visible):
        line_no = scroll + row
        y = body_top + row * line_height
        if changed_range is not None and changed_range[0] <= line_no <= changed_range[1]:
            draw.rectangle((x0 + 1, y, x1 - 1, y + line_height), fill="#3a3416")
        if lexer is not None:
            tokens = [(t, token_color(style, tok)) for tok, t in lex(line_text, lexer)]
        else:
            tokens = [(line_text, theme_fg)]
        draw_line_clipped(draw, tokens, x0 + 6, y, max_width, font, theme_fg)

    if scroll > 0:
        draw.text((x1 - 60, body_top), f"^{scroll}", font=font, fill="#888888")
    if scroll + max_lines < len(lines):
        draw.text((x1 - 60, y1 - line_height), f"v{len(lines) - scroll - max_lines}", font=font, fill="#888888")


def grid_layout(n, width, height, header_h):
    cols = 1
    while cols * cols < n:
        cols += 1
    rows = (n + cols - 1) // cols
    cell_w = width // cols
    cell_h = (height - header_h) // rows
    boxes = []
    for i in range(n):
        r, c = divmod(i, cols)
        x0 = c * cell_w
        y0 = header_h + r * cell_h
        boxes.append((x0, y0, x0 + cell_w, y0 + cell_h))
    return boxes


def render_frame(commit, file_contents, cfg, font, header_font, style, file_states):
    width, height = cfg["width"], cfg["height"]
    theme_bg, theme_fg = "#1e1e1e", "#d4d4d4"
    img = Image.new("RGB", (width, height), theme_bg)
    draw = ImageDraw.Draw(img)

    header_h = header_font.size + 24
    draw.rectangle((0, 0, width, header_h), fill="#0d0d0d")
    dt = datetime.datetime.fromtimestamp(commit["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    header_text = f"{dt}   {commit['hash'][:8]}   {commit['subject'][:100]}"
    draw.text((12, 8), header_text, font=header_font, fill="#ffffff")

    boxes = grid_layout(len(file_contents), width, height, header_h)
    for (filepath, content), box in zip(file_contents.items(), boxes):
        render_panel(draw, filepath, content, box, font, style, theme_bg, theme_fg, file_states[filepath])

    return img


# ---------------------------------------------------------------- video

def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install it, e.g. `brew install ffmpeg`."
        )


def encode_video(frame_dir, concat_entries, output_path, fps):
    concat_path = frame_dir / "concat.txt"
    with open(concat_path, "w") as f:
        for fname, duration in concat_entries:
            f.write(f"file '{fname}'\nduration {duration:.6f}\n")
        # ffmpeg concat demuxer quirk: last file must be repeated without duration
        f.write(f"file '{concat_entries[-1][0]}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-fps_mode", "cfr", "-r", str(fps), "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-2000:]}")


# ---------------------------------------------------------------- main

def load_config(path):
    with open(path) as f:
        user_cfg = yaml.safe_load(f) or {}
    cfg = {**DEFAULTS, **user_cfg}
    if "repo" not in cfg or "files" not in cfg:
        raise RuntimeError("config must include 'repo' and 'files' keys")
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to YAML config file")
    parser.add_argument("--repo", help="override repo URL/path from config")
    parser.add_argument("--output", help="override output video path from config")
    parser.add_argument("--keep-frames", action="store_true", help="don't delete rendered PNG frames")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.repo:
        cfg["repo"] = args.repo
    if args.output:
        cfg["output"] = args.output
    if args.keep_frames:
        cfg["keep_frames"] = True

    check_ffmpeg()

    work_root = Path(cfg["work_dir"]).expanduser() if cfg["work_dir"] else Path(tempfile.mkdtemp(prefix="repo_timelapse_"))
    work_root.mkdir(parents=True, exist_ok=True)
    frame_dir = work_root / "frames"
    frame_dir.mkdir(exist_ok=True)

    try:
        repo_path = clone_or_open_repo(cfg["repo"], work_root, cfg["branch"])
        files = resolve_files(repo_path, cfg["files"])
        commits = collect_commits(repo_path, files, cfg["since"], cfg["until"], cfg["max_commits"])
        if not commits:
            raise RuntimeError("No commits found for the given files/date range.")

        font = get_font(cfg["font_size"])
        header_font = get_font(cfg["font_size"] + 2)
        style = build_style_map(cfg["theme"]) if HAVE_PYGMENTS else None

        concat_entries = []
        duration = cfg["seconds_per_commit"]
        file_states = {f: {"prev_lines": None, "scroll": 0} for f in files}
        rendered = 0
        skipped = 0
        for i, commit in enumerate(commits, 1):
            print(f"\rProcessing commit {i}/{len(commits)} (rendered {rendered})...", end="", flush=True)
            file_contents = {
                f: get_file_content_at_commit(repo_path, commit["hash"], f)
                for f in files
            }
            if all(v is None for v in file_contents.values()):
                # None of the tracked files exist yet at this commit - nothing
                # to show, so skip it instead of padding the video with blanks.
                skipped += 1
                continue
            img = render_frame(commit, file_contents, cfg, font, header_font, style, file_states)
            rendered += 1
            fname = f"frame_{rendered:05d}.png"
            img.save(frame_dir / fname)
            concat_entries.append((fname, duration))
        print()
        if skipped:
            print(f"Skipped {skipped} leading/empty commit(s) where none of the tracked files existed yet.")
        if not concat_entries:
            raise RuntimeError("Nothing to render - none of the tracked files ever existed across the selected commits.")

        print(f"Encoding video -> {cfg['output']}")
        encode_video(frame_dir, concat_entries, Path(cfg["output"]).expanduser(), cfg["fps"])
        print(f"Done: {cfg['output']}")

    finally:
        if not cfg["keep_frames"]:
            shutil.rmtree(work_root, ignore_errors=True)
        else:
            print(f"Frames and clone kept at {work_root}")


if __name__ == "__main__":
    main()
