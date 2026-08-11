#!/usr/bin/env python3
"""Render the deterministic animated terminal walkthrough used by the README."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).parents[1]
OUTPUT = ROOT / "docs" / "assets" / "vps-deployer-demo.gif"
SIZE = (960, 540)
FONT_PATHS = {
    False: (Path("C:/Windows/Fonts/consola.ttf"), "DejaVuSansMono.ttf"),
    True: (Path("C:/Windows/Fonts/consolab.ttf"), "DejaVuSansMono-Bold.ttf"),
}

BG = "#08111f"
PANEL = "#111b2e"
PANEL_EDGE = "#263650"
TEXT = "#e6edf7"
MUTED = "#8ea2bf"
GREEN = "#67e8a5"
CYAN = "#67d5ff"
AMBER = "#f6c85f"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    preferred, fallback = FONT_PATHS[bold]
    return ImageFont.truetype(str(preferred) if preferred.is_file() else fallback, size)


TITLE = font(25, bold=True)
LABEL = font(16, bold=True)
MONO = font(20)
MONO_BOLD = font(20, bold=True)
SMALL = font(15)


SCENES = [
    (
        "PLAN",
        [
            ("$ vps-deployer --repo infra plan example-prod", GREEN),
            ("Deployment: example-prod", TEXT),
            ("Host: prod", MUTED),
            ("", TEXT),
            ("INSTALL release 4d9901a72c81e240", CYAN),
            ("ACTIVATE release 4d9901a72c81e240", CYAN),
            ("RESTART service", CYAN),
        ],
        "Review the complete transaction before mutation",
    ),
    (
        "APPLY",
        [
            ("$ vps-deployer --repo infra apply example-prod", GREEN),
            ("HOST EXPECTATIONS", AMBER),
            ("  OK command systemctl: available", TEXT),
            ("  OK command tar: available", TEXT),
            ("  OK command curl: available", TEXT),
            ("", TEXT),
            ("INSTALL immutable release", CYAN),
            ("ACTIVATE + RESTART + HEALTH CHECK", CYAN),
        ],
        "OpenSSH only - no agent and no container runtime",
    ),
    (
        "VERIFY",
        [
            ("$ vps-deployer --repo infra status example-prod", GREEN),
            ("deployment: example-prod", TEXT),
            ("service: active", GREEN),
            ("active release: 4d9901a72c81e240", TEXT),
            ("health: healthy", GREEN),
            ("manifest: current", GREEN),
            ("", TEXT),
            ("$ vps-deployer --repo infra plan example-prod", GREEN),
            ("No changes.", GREEN),
        ],
        "Active, healthy, attributable, and converged",
    ),
]


def frame(scene_index: int, visible_lines: int) -> Image.Image:
    image = Image.new("RGB", SIZE, BG)
    draw = ImageDraw.Draw(image)
    label, lines, caption = SCENES[scene_index]

    draw.text((48, 32), "NATIVE SYSTEMD DEPLOYMENTS", font=LABEL, fill=CYAN)
    draw.text((48, 58), "PaaS-like release semantics. Your Linux host stays Linux.",
              font=TITLE, fill=TEXT)

    panel = (48, 112, 912, 456)
    draw.rounded_rectangle(panel, radius=14, fill=PANEL, outline=PANEL_EDGE, width=2)
    draw.ellipse((68, 132, 80, 144), fill="#ff6b6b")
    draw.ellipse((88, 132, 100, 144), fill="#ffd166")
    draw.ellipse((108, 132, 120, 144), fill="#67e8a5")
    draw.text((138, 126), "vps-deployer 1.0.1  /  example-prod", font=SMALL, fill=MUTED)

    y = 168
    for text, color in lines[:visible_lines]:
        selected = MONO_BOLD if text.startswith("$") else MONO
        draw.text((72, y), text, font=selected, fill=color)
        y += 30

    progress = ["PLAN", "APPLY", "VERIFY"]
    x = 50
    for index, step in enumerate(progress):
        color = GREEN if index < scene_index else CYAN if index == scene_index else MUTED
        draw.text((x, 480), step, font=LABEL, fill=color)
        x += 92
        if index < len(progress) - 1:
            draw.line((x - 28, 489, x - 8, 489), fill=PANEL_EDGE, width=2)
    draw.text((360, 480), caption, font=SMALL, fill=MUTED)
    draw.text((840, 480), label, font=LABEL, fill=CYAN)
    return image


def render() -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []
    for scene_index, (_, lines, _) in enumerate(SCENES):
        for visible in range(1, len(lines) + 1):
            frames.append(frame(scene_index, visible))
            durations.append(180 if visible < len(lines) else 2100)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    palette = frames[0].convert("P", palette=Image.Palette.ADAPTIVE, colors=128)
    indexed = [item.quantize(palette=palette, dither=Image.Dither.NONE) for item in frames]
    indexed[0].save(OUTPUT, save_all=True, append_images=indexed[1:], duration=durations,
                    loop=0, optimize=True, disposal=2)
    print(f"rendered {OUTPUT} ({len(frames)} frames, {OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    render()
