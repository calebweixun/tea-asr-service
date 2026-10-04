"""Build local listening pages and audio clips for reference transcript review."""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16_000
PAD_SECONDS = 0.3
MIN_FINAL_SECONDS = 2.0
MAX_GROUP_SECONDS = 12.0
MAX_GAP_SECONDS = 2.0
DEFAULT_SOAK_ROOT = Path("/Users/c2leb/Codes/tea-asr-service/.soak")
DEFAULT_OUTPUT = DEFAULT_SOAK_ROOT / "gold"


@dataclass(frozen=True)
class Section:
    name: str
    label: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class ClipSpec:
    name: str
    trace_name: str
    wav_name: str
    sections: tuple[Section, ...]
    music: bool


@dataclass(frozen=True)
class Item:
    id: str
    start_s: float
    end_s: float
    draft: str
    section: str
    audio_path: str


CLIP_SPECS = (
    ClipSpec(
        name="speakers",
        trace_name="church-600.jsonl",
        wav_name="church-30m-59m.wav",
        sections=(
            Section("young_woman", "年輕女性", 0, 120),
            Section("elderly_woman", "年長女性", 120, 180),
            Section("man", "男性", 180, 360),
        ),
        music=False,
    ),
    *(
        ClipSpec(
            name=f"music-{clip_id}",
            trace_name=f"music-{clip_id}.jsonl",
            wav_name=f"music-{clip_id}.wav",
            sections=(Section(f"music-{clip_id}", "音樂重疊", 0, 180),),
            music=True,
        )
        for clip_id in ("0123", "0934", "1339", "8900")
    ),
)


def read_trace_finals(trace_path: Path) -> list[dict[str, Any]]:
    """Read transcript.final events, tolerating a partially written last line."""
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    finals: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if line_number == len(lines):
                break
            raise ValueError(f"Invalid JSON in {trace_path} at line {line_number}")
        event = row.get("event")
        if not isinstance(event, dict) or event.get("type") != "transcript.final":
            continue
        if not isinstance(event.get("text"), str):
            continue
        start = event.get("start_sample")
        end = event.get("end_sample")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        if end > start:
            finals.append(event)
    return sorted(finals, key=lambda event: (event["start_sample"], event["end_sample"]))


def group_finals(
    finals: list[dict[str, Any]],
    *,
    sample_rate: int = SAMPLE_RATE,
    min_final_seconds: float = MIN_FINAL_SECONDS,
    max_group_seconds: float = MAX_GROUP_SECONDS,
    max_gap_seconds: float = MAX_GAP_SECONDS,
) -> list[list[dict[str, Any]]]:
    """Keep normal finals whole and pack neighboring short finals into review clips."""
    ordered = sorted(finals, key=lambda event: (event["start_sample"], event["end_sample"]))
    groups: list[list[dict[str, Any]]] = []
    short_group: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal short_group
        if short_group:
            groups.append(short_group)
            short_group = []

    for event in ordered:
        start = int(event["start_sample"])
        end = int(event["end_sample"])
        duration = (end - start) / sample_rate
        if duration >= min_final_seconds:
            flush()
            groups.append([event])
            continue
        if short_group:
            previous_end = max(int(item["end_sample"]) for item in short_group)
            group_start = min(int(item["start_sample"]) for item in short_group)
            combined_end = max(end, previous_end)
            gap = max(0, start - previous_end) / sample_rate
            span = (combined_end - group_start) / sample_rate
            if gap > max_gap_seconds or span > max_group_seconds:
                flush()
        short_group.append(event)
    flush()
    return groups


def join_final_text(finals: list[dict[str, Any]]) -> str:
    """Join Chinese finals directly while preserving boundaries between Latin words."""
    text = ""
    for event in finals:
        part = str(event.get("text", "")).strip()
        if not part:
            continue
        if text and text[-1].isascii() and text[-1].isalnum() and part[0].isascii() and part[0].isalnum():
            text += " "
        text += part
    return text


def section_for_event(event: dict[str, Any], sections: tuple[Section, ...]) -> Section | None:
    midpoint = (int(event["start_sample"]) + int(event["end_sample"])) / (2 * SAMPLE_RATE)
    return next((section for section in sections if section.start_s <= midpoint < section.end_s), None)


def make_items(
    finals: list[dict[str, Any]], spec: ClipSpec, wav_duration_s: float
) -> list[Item]:
    assigned: dict[str, list[dict[str, Any]]] = {section.name: [] for section in spec.sections}
    for event in finals:
        section = section_for_event(event, spec.sections)
        if section is None:
            continue
        event = dict(event)
        event["start_sample"] = max(
            int(event["start_sample"]), int(section.start_s * SAMPLE_RATE)
        )
        event["end_sample"] = min(int(event["end_sample"]), int(section.end_s * SAMPLE_RATE))
        if event["end_sample"] > event["start_sample"]:
            assigned[section.name].append(event)

    grouped: list[tuple[Section, list[dict[str, Any]]]] = []
    for section in spec.sections:
        grouped.extend((section, group) for group in group_finals(assigned[section.name]))
    grouped.sort(key=lambda pair: min(int(e["start_sample"]) for e in pair[1]))

    items: list[Item] = []
    for index, (section, group) in enumerate(grouped, start=1):
        start_sample = min(int(event["start_sample"]) for event in group)
        end_sample = max(int(event["end_sample"]) for event in group)
        start_s = start_sample / SAMPLE_RATE
        end_s = end_sample / SAMPLE_RATE
        clip_start_s = max(0.0, start_s - PAD_SECONDS)
        clip_end_s = min(wav_duration_s, end_s + PAD_SECONDS)
        if clip_end_s <= clip_start_s:
            continue
        items.append(
            Item(
                id=f"{spec.name}-{index:04d}",
                start_s=start_s,
                end_s=end_s,
                draft=join_final_text(group),
                section=section.label,
                audio_path=f"audio/{spec.name}-{index:04d}.m4a",
            )
        )
    return items


def run_ffmpeg(
    ffmpeg: str, wav_path: Path, output_path: Path, start_s: float, end_s: float
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.0, end_s - start_s)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(wav_path),
        "-ss",
        f"{start_s:.6f}",
        "-t",
        f"{duration:.6f}",
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(
            f"ffmpeg failed for {output_path}: {result.stderr[-1500:].strip()}"
        )


def html_document(set_name: str, items: list[Item], music: bool) -> str:
    data = [
        {
            "id": item.id,
            "start_s": round(item.start_s, 3),
            "end_s": round(item.end_s, 3),
            "draft": item.draft,
            "section": item.section,
            "audio": item.audio_path,
            "music": music,
        }
        for item in items
    ]
    encoded_data = (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    title = html.escape(set_name)
    return f'''<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>聆聽校稿｜{title}</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Noto Sans TC", sans-serif; color: #263238; background: #f4f6f5; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 18px max(20px, calc((100vw - 920px) / 2)); background: #f4f6f5ee; border-bottom: 1px solid #dce4e0; backdrop-filter: blur(10px); }}
    h1 {{ margin: 0; font-size: 20px; font-weight: 650; }}
    .sub {{ margin-top: 5px; color: #60716b; font-size: 13px; }}
    main {{ max-width: 920px; margin: 22px auto 72px; padding: 0 18px; }}
    .toolbar {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }}
    button {{ border: 0; border-radius: 10px; padding: 11px 16px; color: #fff; background: #286957; font: inherit; font-weight: 600; cursor: pointer; }}
    button:hover {{ background: #1f5748; }}
    #save-status {{ color: #63746d; font-size: 13px; }}
    .hint {{ margin-left: auto; color: #63746d; font-size: 12px; }}
    .card {{ margin: 12px 0; padding: 17px; border: 1px solid #dce4e0; border-radius: 14px; background: #fff; box-shadow: 0 2px 8px #23352d08; scroll-margin-top: 110px; }}
    .card.active {{ border-color: #70a792; box-shadow: 0 0 0 2px #70a79220; }}
    .row-head {{ display: flex; align-items: center; gap: 9px; margin-bottom: 10px; }}
    .number {{ color: #71817b; font-size: 13px; font-variant-numeric: tabular-nums; }}
    .section {{ padding: 3px 8px; border-radius: 999px; color: #376b5c; background: #eaf4ef; font-size: 12px; }}
    .time {{ margin-left: auto; color: #71817b; font-size: 12px; font-variant-numeric: tabular-nums; }}
    audio {{ display: block; width: 100%; height: 38px; margin: 4px 0 12px; }}
    label.field-title {{ display: block; margin-bottom: 6px; color: #50615a; font-size: 13px; }}
    textarea {{ display: block; width: 100%; min-height: 92px; resize: vertical; border: 1px solid #cbd7d1; border-radius: 9px; padding: 11px 12px; color: #263238; background: #fcfdfc; font: inherit; font-size: 15px; line-height: 1.65; }}
    textarea:focus {{ outline: 2px solid #90b9a9; border-color: transparent; }}
    .checks {{ display: flex; gap: 22px; flex-wrap: wrap; margin-top: 11px; color: #52635c; font-size: 13px; }}
    .checks label {{ display: flex; align-items: center; gap: 7px; cursor: pointer; }}
    input[type="checkbox"] {{ accent-color: #286957; width: 16px; height: 16px; }}
    .empty {{ padding: 28px; text-align: center; color: #65766f; background: #fff; border: 1px solid #dce4e0; border-radius: 14px; }}
    @media (max-width: 600px) {{ header {{ padding: 15px 18px; }} .hint {{ width: 100%; margin-left: 0; }} .card {{ padding: 14px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>聆聽校稿｜{title}</h1>
    <div class="sub">聽一段、修正文稿；內容會自動保存在這個瀏覽器。</div>
  </header>
  <main>
    <div class="toolbar">
      <button id="download" type="button">下載答案 JSON</button>
      <span id="save-status" role="status" aria-live="polite">尚未修改</span>
      <span class="hint">快捷鍵：空白鍵播放目前段落・Enter 前往下一段</span>
    </div>
    <div id="items"></div>
  </main>
  <script>
    const initialItems = {encoded_data};
    const setName = {json.dumps(set_name, ensure_ascii=False)};
    const storageKey = "tea-asr-gold:" + setName;
    const container = document.getElementById("items");
    const status = document.getElementById("save-status");
    const rows = [];
    let activeIndex = 0;
    let storageAvailable = true;

    function currentAnswers() {{
      return {{set: setName, items: rows.map((row) => ({{
        id: row.item.id,
        start_s: row.item.start_s,
        end_s: row.item.end_s,
        draft: row.item.draft,
        reference: row.textarea.value,
        music: row.music.checked,
        unclear: row.unclear.checked
      }}))}};
    }}
    function save() {{
      if (!storageAvailable) return;
      try {{
        localStorage.setItem(storageKey, JSON.stringify(currentAnswers()));
        status.textContent = "已自動保存";
      }} catch (error) {{
        storageAvailable = false;
        status.textContent = "瀏覽器未開放本機保存；下載 JSON 可保留答案";
      }}
    }}
    function activate(index, focusText = false) {{
      if (!rows.length) return;
      activeIndex = Math.max(0, Math.min(index, rows.length - 1));
      rows.forEach((row, i) => row.card.classList.toggle("active", i === activeIndex));
      rows[activeIndex].card.scrollIntoView({{behavior: "smooth", block: "nearest"}});
      if (focusText) rows[activeIndex].textarea.focus({{preventScroll: true}});
    }}
    function playCurrent() {{
      if (!rows.length) return;
      const audio = rows[activeIndex].audio;
      audio.currentTime = 0;
      audio.play().catch(() => {{ status.textContent = "無法播放此片段，請確認音檔存在"; }});
    }}
    function makeRow(item, index) {{
      const card = document.createElement("section");
      card.className = "card";
      card.tabIndex = -1;
      card.setAttribute("aria-label", `第 ${{index + 1}} 段`);
      card.addEventListener("click", () => activate(index));
      const head = document.createElement("div");
      head.className = "row-head";
      const number = document.createElement("span");
      number.className = "number";
      number.textContent = `第 ${{index + 1}} / ${{initialItems.length}} 段`;
      const section = document.createElement("span");
      section.className = "section";
      section.textContent = item.section;
      const time = document.createElement("span");
      time.className = "time";
      time.textContent = `${{item.start_s.toFixed(1)}}–${{item.end_s.toFixed(1)}} 秒`;
      head.append(number, section, time);
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.preload = "none";
      const source = document.createElement("source");
      source.src = item.audio;
      source.type = "audio/mp4";
      audio.append(source);
      const title = document.createElement("label");
      title.className = "field-title";
      title.textContent = "ASR 草稿（可直接修改）";
      const textarea = document.createElement("textarea");
      textarea.setAttribute("aria-label", `第 ${{index + 1}} 段校正文字`);
      textarea.spellcheck = false;
      textarea.value = item.draft;
      title.htmlFor = `text-${{index}}`;
      textarea.id = `text-${{index}}`;
      const checks = document.createElement("div");
      checks.className = "checks";
      const musicLabel = document.createElement("label");
      const music = document.createElement("input");
      music.type = "checkbox";
      music.checked = Boolean(item.music);
      musicLabel.append(music, document.createTextNode("音樂干擾"));
      const unclearLabel = document.createElement("label");
      const unclear = document.createElement("input");
      unclear.type = "checkbox";
      unclearLabel.append(unclear, document.createTextNode("聽不清楚"));
      checks.append(musicLabel, unclearLabel);
      card.append(head, audio, title, textarea, checks);
      for (const field of [textarea, music, unclear]) field.addEventListener("input", save);
      for (const field of [music, unclear]) field.addEventListener("change", save);
      textarea.addEventListener("focus", () => activate(index));
      audio.addEventListener("play", () => activate(index));
      container.append(card);
      rows.push({{item, card, audio, textarea, music, unclear}});
    }}
    if (!initialItems.length) {{
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "目前沒有可校稿的 transcript.final 片段。";
      container.append(empty);
    }}
    initialItems.forEach(makeRow);
    try {{
      const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (saved && Array.isArray(saved.items)) {{
        const byId = new Map(saved.items.map((item) => [item.id, item]));
        for (const row of rows) {{
          const old = byId.get(row.item.id);
          if (!old) continue;
          if (typeof old.reference === "string") row.textarea.value = old.reference;
          row.music.checked = Boolean(old.music);
          row.unclear.checked = Boolean(old.unclear);
        }}
        status.textContent = "已載入先前保存的答案";
      }}
    }} catch (error) {{
      storageAvailable = false;
      status.textContent = "瀏覽器未開放本機保存；下載 JSON 可保留答案";
    }}
    document.getElementById("download").addEventListener("click", () => {{
      const blob = new Blob([JSON.stringify(currentAnswers(), null, 2)], {{type: "application/json;charset=utf-8"}});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${{setName}}-answers.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      save();
      status.textContent = "答案 JSON 已下載";
    }});
    document.addEventListener("keydown", (event) => {{
      if (event.isComposing || event.keyCode === 229) return;
      const target = event.target instanceof Element ? event.target : null;
      if (event.key === " " && !(target && target.closest("textarea, input, button, audio"))) {{
        event.preventDefault();
        playCurrent();
      }} else if (event.key === "Enter") {{
        event.preventDefault();
        activate(activeIndex + 1, true);
      }}
    }});
    if (rows.length) activate(0);
  </script>
</body>
</html>
'''


def probe_duration(ffmpeg: str, wav_path: Path) -> float:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
    if not Path(ffprobe).exists():
        raise FileNotFoundError(f"ffprobe not found next to ffmpeg: {ffprobe}")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(wav_path)],
        text=True,
        capture_output=True,
        check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def build(args: argparse.Namespace) -> list[dict[str, Any]]:
    soak_root: Path = args.soak_root
    output_root: Path = args.output
    if not Path(args.ffmpeg).exists():
        raise FileNotFoundError(f"ffmpeg not found: {args.ffmpeg}")

    selected = set(args.sets or [])
    summaries: list[dict[str, Any]] = []
    for spec in CLIP_SPECS:
        if selected and spec.name not in selected:
            continue
        trace_path = soak_root / "traces" / spec.trace_name
        wav_path = soak_root / "audio" / spec.wav_name
        if not trace_path.exists():
            print(f"SKIP {spec.name}: trace is not present", file=sys.stderr)
            continue
        if not wav_path.exists():
            print(f"SKIP {spec.name}: WAV is not present", file=sys.stderr)
            continue
        finals = read_trace_finals(trace_path)
        duration = probe_duration(args.ffmpeg, wav_path)
        items = make_items(finals, spec, duration)
        set_root = output_root / spec.name
        for item in items:
            output_path = set_root / item.audio_path
            start_s = max(0.0, item.start_s - PAD_SECONDS)
            end_s = min(duration, item.end_s + PAD_SECONDS)
            run_ffmpeg(args.ffmpeg, wav_path, output_path, start_s, end_s)
        set_root.mkdir(parents=True, exist_ok=True)
        (set_root / "index.html").write_text(
            html_document(spec.name, items, spec.music), encoding="utf-8"
        )
        audio_minutes = sum(
            (min(duration, item.end_s + PAD_SECONDS) - max(0.0, item.start_s - PAD_SECONDS))
            for item in items
        ) / 60
        summary = {
            "set": spec.name,
            "index": str(set_root / "index.html"),
            "items": len(items),
            "audio_minutes": round(audio_minutes, 2),
        }
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False))
    return summaries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="cut clips and create local HTML pages")
    build_parser.add_argument("--soak-root", type=Path, default=DEFAULT_SOAK_ROOT)
    build_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    build_parser.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")
    build_parser.add_argument("--set", dest="sets", action="append", choices=[s.name for s in CLIP_SPECS])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        build(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
