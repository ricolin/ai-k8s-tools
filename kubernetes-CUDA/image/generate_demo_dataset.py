from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter


PALETTES = (
    ("lake", (35, 112, 142), (242, 170, 92), (35, 63, 73)),
    ("rain", (65, 100, 126), (181, 128, 97), (46, 55, 65)),
    ("alpine", (68, 126, 102), (219, 157, 112), (48, 73, 68)),
    ("dawn", (123, 92, 151), (232, 155, 117), (59, 67, 91)),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def watercolor_background(seed: int, palette: tuple) -> Image.Image:
    rng = random.Random(seed)
    image = Image.new("RGB", (1024, 1024), (242, 238, 226))
    for color in palette[1:]:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for _ in range(65):
            x = rng.randint(-160, 980)
            y = rng.randint(-160, 980)
            width = rng.randint(100, 420)
            height = rng.randint(60, 300)
            varied = tuple(max(0, min(255, channel + rng.randint(-22, 22))) for channel in color)
            draw.ellipse((x, y, x + width, y + height), fill=(*varied, rng.randint(18, 48)))
        layer = layer.filter(ImageFilter.GaussianBlur(rng.randint(18, 45)))
        image = Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")
    return image


def add_paper_grain(image: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    grain = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(grain)
    for _ in range(9000):
        value = rng.randint(210, 252)
        alpha = rng.randint(3, 13)
        x = rng.randrange(image.width)
        y = rng.randrange(image.height)
        draw.point((x, y), fill=(value, value, value, alpha))
    return Image.alpha_composite(image.convert("RGBA"), grain).convert("RGB")


def add_impressionist_light(image: Image.Image, seed: int, palette: tuple) -> Image.Image:
    rng = random.Random(seed)
    image = ImageEnhance.Color(image).enhance(1.18)
    image = ImageEnhance.Contrast(image).enhance(1.08)
    strokes = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(strokes)
    source = image.load()
    accent_colors = list(palette[1:]) + [(245, 215, 128), (118, 166, 183), (198, 117, 105)]
    for _ in range(7200):
        x = rng.randrange(image.width)
        y = rng.randrange(image.height)
        sampled = source[x, y]
        accent = accent_colors[rng.randrange(len(accent_colors))]
        mix = rng.uniform(0.12, 0.38)
        color = tuple(round(sampled[channel] * (1 - mix) + accent[channel] * mix) for channel in range(3))
        varied = tuple(max(0, min(255, channel + rng.randint(-24, 24))) for channel in color)
        length = rng.randint(8, 34)
        width = rng.randint(3, 11)
        direction = -1 if (x + y + seed) % 3 == 0 else 1
        draw.line(
            (x, y, x + direction * length, y + rng.randint(-6, 6)),
            fill=(*varied, rng.randint(70, 150)),
            width=width,
        )
    for _ in range(420):
        x = rng.randrange(image.width)
        y = rng.randrange(image.height)
        radius = rng.randint(8, 30)
        sampled = source[x, y]
        draw.ellipse(
            (x - radius, y - radius // 2, x + radius, y + radius // 2),
            fill=(*sampled, rng.randint(18, 48)),
        )
    return Image.alpha_composite(image.convert("RGBA"), strokes).convert("RGB")


def draw_scene(image: Image.Image, seed: int, style: str) -> None:
    rng = random.Random(seed)
    draw = ImageDraw.Draw(image, "RGBA")
    horizon = rng.randint(390, 570)
    impressionism = style == "B-impressionism"
    detail = style == "B-detail"
    ink = (25, 40, 49, 65 if impressionism else (150 if detail else 95))
    draw.polygon([(0, horizon), (250, horizon - 230), (470, horizon), (730, horizon - 300), (1024, horizon)], fill=(53, 83, 76, 105))
    draw.rectangle((0, horizon, 1024, 1024), fill=(50, 122, 151, 72))
    building_x = rng.randint(110, 210)
    building_y = rng.randint(245, 330)
    building_w = rng.randint(620, 760)
    building_h = rng.randint(360, 470)
    draw.rectangle((building_x, building_y, building_x + building_w, building_y + building_h), fill=(220, 229, 221, 155), outline=ink, width=4)
    columns = 12 if detail else (9 if impressionism else 7)
    rows = 6 if detail else (5 if impressionism else 4)
    gap_x = building_w / columns
    gap_y = building_h / rows
    for column in range(1, columns):
        x = round(building_x + column * gap_x)
        draw.line((x, building_y, x, building_y + building_h), fill=ink, width=2 if detail else 3)
    for row in range(1, rows):
        y = round(building_y + row * gap_y)
        draw.line((building_x, y, building_x + building_w, y), fill=ink, width=2)
    if detail:
        for rack in range(16):
            x = building_x + 18 + rack * max(20, (building_w - 40) // 16)
            draw.rectangle((x, building_y + 40, x + 21, building_y + building_h - 38), outline=(28, 44, 54, 185), width=2)
            for panel in range(9):
                y = building_y + 53 + panel * 31
                draw.line((x + 3, y, x + 18, y), fill=(28, 44, 54, 130), width=1)
        for cable in range(22):
            start_x = building_x + rng.randint(0, building_w)
            control_y = building_y - rng.randint(25, 120)
            end_x = building_x + rng.randint(0, building_w)
            draw.arc((min(start_x, end_x), control_y, max(start_x, end_x) + 30, building_y + 120), 180, 350, fill=(45, 58, 66, 155), width=2)
        for pipe in range(8):
            y = building_y + 20 + pipe * 38
            draw.line((building_x - 55, y, building_x + building_w + 55, y), fill=(92, 77, 67, 130), width=3)
    for _ in range(35 if detail else (28 if impressionism else 18)):
        x = rng.randint(0, 1024)
        y = rng.randint(horizon, 1000)
        draw.line((x, y, x + rng.randint(15, 80), y + rng.randint(-8, 8)), fill=(30, 77, 95, rng.randint(35, 85)), width=1)


def write_stage(root: Path, stage: str, count: int, seed: int) -> list[dict]:
    records: list[dict] = []
    detail = stage == "B-detail"
    impressionism = stage == "B-impressionism"
    for index in range(count):
        palette = PALETTES[index % len(PALETTES)]
        item_seed = seed + (20000 if impressionism else (10000 if detail else 0)) + index
        image = watercolor_background(item_seed, palette)
        draw_scene(image, item_seed, stage)
        image = add_paper_grain(image, item_seed + 30000)
        if impressionism:
            image = add_impressionist_light(image, item_seed + 40000, palette)
        relative = Path("images") / stage.lower() / f"{index:03d}.png"
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="PNG", optimize=True)
        split = "validation" if index % 6 == 0 else "train"
        if impressionism:
            caption = (
                "abt_watercolor, abt_impressionism, an architectural watercolor transformed through historical "
                "Impressionist painting qualities, broken color, visible brush dabs, luminous atmospheric light, "
                "softened contours, reflected color, layered pigment, and textured paper"
            )
        elif detail:
            caption = (
                "abt_watercolor_detail, a precise watercolor architectural scene with fine structural linework, "
                "server-rack panels, cables, pipes, transparent surfaces, material texture, and consistent depth"
            )
        else:
            caption = (
                "abt_watercolor, a luminous architectural watercolor with transparent layered washes, pigment "
                "blooms, wet edges, granulating color, visible cold-pressed paper texture, and balanced depth"
            )
        records.append(
            {
                "id": f"{stage.lower()}-{index:03d}",
                "path": relative.as_posix(),
                "caption": caption,
                "source": f"deterministic-procedural-generator seed={item_seed} palette={palette[0]}",
                "license": "CC0-1.0",
                "permission_confirmed": True,
                "sha256": sha256_file(destination),
                "stage": stage,
                "split": split,
                "sampling_weight": 1,
            }
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a deterministic SDXL watercolor/Impressionist A/B dataset")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--a-count", type=int, default=96)
    parser.add_argument("--b-count", type=int, default=96)
    parser.add_argument("--replay-count", type=int, default=24)
    parser.add_argument("--b-style", choices=("impressionism", "detail"), default="impressionism")
    parser.add_argument("--seed", type=int, default=260820)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output already exists")
    if not (24 <= args.a_count <= 512 and 24 <= args.b_count <= 512):
        raise SystemExit("A and B counts must each be between 24 and 512")
    if not 0 <= args.replay_count <= args.a_count:
        raise SystemExit("replay count must be between zero and the A count")
    args.output.mkdir(parents=True)
    records = write_stage(args.output, "A", args.a_count, args.seed)
    b_stage = "B-impressionism" if args.b_style == "impressionism" else "B-detail"
    records.extend(write_stage(args.output, b_stage, args.b_count, args.seed))
    replay_sources = [record for record in records if record["stage"] == "A" and record["split"] == "train"]
    for record in replay_sources[: args.replay_count]:
        records.append({**record, "id": record["id"].replace("a-", "a-replay-", 1), "stage": "A-replay"})
    manifest = args.output / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))
    summary = {
        "schema_version": "1.0.0",
        "generator": "ai-k8s-tools deterministic procedural watercolor and Impressionist demo",
        "seed": args.seed,
        "a_count": args.a_count,
        "b_stage": b_stage,
        "b_count": args.b_count,
        "a_replay_count": min(args.replay_count, len(replay_sources)),
        "manifest_digest": f"sha256:{sha256_file(manifest)}",
        "proof_boundary": "synthetic demonstration data; not a substitute for a curated production dataset",
    }
    (args.output / "dataset-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
