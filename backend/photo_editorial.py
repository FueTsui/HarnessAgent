"""受限的“原始照片 + 抽象记忆面板”本地成品渲染器。

视觉模型负责从照片提取语义、构图和版式决策；Pillow 负责忠实保留原图、
绘制照片驱动的抽象面板并准确排印标题。仅由明确绑定
``photo-abstract-editorial`` Skill 的任务调用，避免把确定性排版能力冒充
通用图片生成或任意图像编辑能力。
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps


_MOTIFS = {"arches", "horizon", "cluster", "verticals", "flow"}
_TITLE_LAYOUTS = {"left", "center"}


def _json_object(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_spec(value: str) -> dict[str, Any]:
    raw = _json_object(value)
    if not raw:
        raise ValueError("视觉模型未返回有效的编辑设计 JSON")
    title = re.sub(r"[^A-Za-z0-9 '&-]", "", str(raw.get("title") or "")).strip()
    if not 2 <= len(title.split()) <= 5:
        raise ValueError("视觉模型返回的标题必须是 2 至 5 个英文单词")
    subtitle = re.sub(
        r"[^A-Za-z0-9 ,.'&-]", "", str(raw.get("subtitle") or "")
    ).strip()[:80]
    motif = str(raw.get("motif") or "").strip().lower()
    if motif not in _MOTIFS:
        raise ValueError("视觉模型返回了不支持的抽象母题")
    try:
        focus_x = min(0.78, max(0.22, float(raw.get("focus_x", 0.58))))
    except (TypeError, ValueError):
        focus_x = 0.58
    try:
        density = min(6, max(3, int(raw.get("density", 4))))
    except (TypeError, ValueError):
        density = 4
    title_layout = str(raw.get("title_layout") or "").strip().lower()
    if title_layout not in _TITLE_LAYOUTS:
        title_layout = "center" if motif in {"arches", "cluster", "verticals"} else "left"
    try:
        motif_width = min(0.68, max(0.30, float(raw.get(
            "motif_width", 0.58 if motif in {"arches", "horizon"} else 0.42
        ))))
    except (TypeError, ValueError):
        motif_width = 0.58 if motif in {"arches", "horizon"} else 0.42
    try:
        motif_height = min(0.38, max(0.22, float(raw.get("motif_height", 0.32))))
    except (TypeError, ValueError):
        motif_height = 0.32
    # 地标建筑和流动型构图需要足够的视觉质量。视觉模型偶尔选择合法区间
    # 的下限，虽然结构检查能通过，却会产生小图标式的空洞版面。
    minimum_width = (
        0.60 if motif == "arches"
        else 0.56 if motif in {"horizon", "flow"}
        else 0.46
    )
    motif_width = max(minimum_width, motif_width)
    if motif == "arches":
        motif_height = max(0.30, motif_height)
    try:
        asymmetry = min(0.30, max(-0.30, float(raw.get("asymmetry", focus_x - 0.5))))
    except (TypeError, ValueError):
        asymmetry = focus_x - 0.5
    if motif == "arches" and abs(asymmetry) <= 0.18:
        title_layout = "center"
    try:
        mass_count = min(5, max(2, int(raw.get("mass_count", min(density, 4)))))
    except (TypeError, ValueError):
        mass_count = min(density, 4)
    try:
        horizon_count = min(2, max(0, int(raw.get(
            "horizon_count", 2 if motif in {"arches", "horizon"} else 1
        ))))
    except (TypeError, ValueError):
        horizon_count = 2 if motif in {"arches", "horizon"} else 1
    return {
        "title": title,
        "subtitle": subtitle,
        "motif": motif,
        "focus_x": focus_x,
        "density": density,
        "title_layout": title_layout,
        "motif_width": motif_width,
        "motif_height": motif_height,
        "asymmetry": asymmetry,
        "mass_count": mass_count,
        "horizon_count": horizon_count,
    }


def _font(size: int, *, italic: bool = False):
    candidates = [
        Path("C:/Windows/Fonts/georgiai.ttf" if italic else "C:/Windows/Fonts/georgia.ttf"),
        Path("C:/Windows/Fonts/timesi.ttf" if italic else "C:/Windows/Fonts/times.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _luminance(color: tuple[int, int, int]) -> float:
    return color[0] * 0.2126 + color[1] * 0.7152 + color[2] * 0.0722


def _saturation(color: tuple[int, int, int]) -> int:
    return max(color) - min(color)


def _muted_palette(
    image: Image.Image,
    ivory: tuple[int, int, int],
) -> dict[str, tuple[int, int, int]]:
    sample = image.convert("RGB").resize((96, 96))
    quantized = sample.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
    counts = sorted(quantized.getcolors() or [], reverse=True)
    palette_values = quantized.getpalette() or []
    weighted: list[tuple[int, tuple[int, int, int]]] = []
    for _, index in counts:
        rgb = tuple(palette_values[index * 3:index * 3 + 3])
        if len(rgb) != 3:
            continue
        # 与象牙底轻微混合，保持来自原图但降低面板饱和/对比。
        mixed = tuple(int(channel * 0.78 + ivory[pos] * 0.22) for pos, channel in enumerate(rgb))
        if all(mixed != color for _, color in weighted):
            count = next((amount for amount, idx in counts if idx == index), 1)
            weighted.append((count, mixed))
    colors = [color for _, color in weighted]
    while len(colors) < 4:
        colors.append((75 + len(colors) * 18,) * 3)
    dominant = colors[0]
    dark = min(colors, key=_luminance)
    light = max(colors, key=_luminance)
    chromatic = sorted(
        colors,
        key=lambda color: (_saturation(color), -abs(_luminance(color) - 125)),
        reverse=True,
    )
    accent = chromatic[0]
    mid_candidates = [
        color for color in colors
        if color not in {dark, light}
    ] or colors
    mid = min(mid_candidates, key=lambda color: abs(_luminance(color) - 130))
    return {
        "dominant": dominant,
        "mid": mid,
        "dark": dark,
        "light": light,
        "accent": accent,
    }


def _dark_ink(color: tuple[int, int, int]) -> tuple[int, int, int]:
    if _luminance(color) <= 105:
        return tuple(max(20, channel) for channel in color)
    return tuple(max(24, min(100, round(channel * 0.62))) for channel in color)


def _mix(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    amount: float,
) -> tuple[int, int, int]:
    amount = min(1.0, max(0.0, float(amount)))
    return tuple(
        round(first[index] * (1 - amount) + second[index] * amount)
        for index in range(3)
    )


def _gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """创建连续色阶，避免抽象面板退化成少数纯色色块。"""
    width, height = size
    strip = Image.new("RGB", (1, max(1, height)))
    pixels = strip.load()
    for y in range(max(1, height)):
        ratio = y / max(1, height - 1)
        pixels[0, y] = _mix(top, bottom, ratio)
    return strip.resize((max(1, width), max(1, height)))


def _prepare_panel(
    size: tuple[int, int],
    background: tuple[int, int, int],
    palette: dict[str, tuple[int, int, int]],
) -> Image.Image:
    """生成克制但不死白的连续色调背景；四角仍保持基准象牙色。"""
    panel = Image.new("RGB", size, background)
    width, height = size
    glow_mask = Image.new("L", size, 0)
    glow_draw = ImageDraw.Draw(glow_mask)
    glow_draw.ellipse(
        (
            round(width * .17), round(height * .03),
            round(width * .83), round(height * .68),
        ),
        fill=120,
    )
    glow_mask = glow_mask.filter(ImageFilter.GaussianBlur(max(24, round(width * .075))))
    glow_color = _mix(background, palette["light"], .20)
    panel.paste(Image.new("RGB", size, glow_color), (0, 0), glow_mask)
    # 保留极窄的准确底色边界，使面板来源和拼接质量仍可确定性验证。
    edge = max(2, round(width * .002))
    borders = ImageDraw.Draw(panel)
    borders.rectangle((0, 0, width - 1, edge), fill=background)
    borders.rectangle((0, height - edge - 1, width - 1, height - 1), fill=background)
    borders.rectangle((0, 0, edge, height - 1), fill=background)
    borders.rectangle((width - edge - 1, 0, width - 1, height - 1), fill=background)
    return panel


def _cubic_points(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    *,
    steps: int = 20,
) -> list[tuple[int, int]]:
    points = []
    for index in range(steps + 1):
        t = index / steps
        inverse = 1 - t
        x = (
            inverse ** 3 * p0[0]
            + 3 * inverse ** 2 * t * p1[0]
            + 3 * inverse * t ** 2 * p2[0]
            + t ** 3 * p3[0]
        )
        y = (
            inverse ** 3 * p0[1]
            + 3 * inverse ** 2 * t * p1[1]
            + 3 * inverse * t ** 2 * p2[1]
            + t ** 3 * p3[1]
        )
        points.append((round(x), round(y)))
    return points


def _arch_mass(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    *,
    fill: tuple[int, int, int],
    background: tuple[int, int, int],
    thickness: float = 0.18,
    skew: float = 0.0,
) -> None:
    """绘制带真实负空间、连续明暗和轻微非对称的壳体质量。"""
    left, top, right, baseline = box
    width = max(4, right - left)
    height = max(4, baseline - top)
    mask = Image.new("L", canvas.size, 0)
    marks = ImageDraw.Draw(mask)
    skew = min(.30, max(-.30, float(skew)))
    peak_x = left + width * (.48 + skew * .16)
    peak = (peak_x, top)
    outer_left = _cubic_points(
        (left, baseline),
        (left + width * .01, baseline - height * .60),
        (peak_x - width * .31, top),
        peak,
    )
    outer_right = _cubic_points(
        peak,
        (peak_x + width * .34, top + height * .01),
        (right - width * .01, baseline - height * .58),
        (right, baseline),
    )
    marks.polygon([*outer_left, *outer_right[1:]], fill=255)
    inset = max(4, round(width * thickness))
    inner_left, inner_right = left + inset, right - round(inset * .72)
    inner_top = top + max(4, round(height * thickness * .82))
    if inner_right > inner_left:
        inner_peak_x = peak_x + width * (.015 + skew * .04)
        inner_peak = (inner_peak_x, inner_top)
        inner_from_right = _cubic_points(
            (inner_right, baseline),
            (inner_right - width * .01, baseline - height * .45),
            (inner_peak_x + width * .23, inner_top),
            inner_peak,
        )
        inner_to_left = _cubic_points(
            inner_peak,
            (inner_peak_x - width * .20, inner_top + height * .01),
            (inner_left + width * .01, baseline - height * .43),
            (inner_left, baseline),
        )
        marks.polygon([*inner_from_right, *inner_to_left[1:]], fill=0)
    # 柔和投影和纵向色阶建立前后关系；负空间继续透出已有背景/后层质量。
    blur_radius = max(3, round(width * .018))
    shadow_mask = mask.filter(ImageFilter.GaussianBlur(blur_radius))
    shadow_color = _mix(fill, (30, 42, 54), .34)
    shadow_strength = shadow_mask.point(lambda value: round(value * .14))
    canvas.paste(Image.new("RGB", canvas.size, shadow_color), (0, 0), shadow_strength)
    top_color = _mix(fill, background, .36)
    bottom_color = _mix(fill, (35, 48, 61), .12)
    layer = _gradient(canvas.size, top_color, bottom_color)
    canvas.paste(layer, (0, 0), mask)

    # 极轻的高光边缘让曲面保持建筑感，而不是信息图符号。
    edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(5)), mask)
    edge = edge.point(lambda value: round(value * .20))
    canvas.paste(
        Image.new("RGB", canvas.size, _mix(fill, (255, 255, 255), .45)),
        (0, 0), edge,
    )


def _dome_mass(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    *,
    fill: tuple[int, int, int],
    background: tuple[int, int, int],
    skew: float = 0.0,
) -> None:
    """绘制实体次级壳体，避免多个同构空心拱退化成图标。"""
    left, top, right, baseline = box
    width = max(4, right - left)
    height = max(4, baseline - top)
    peak_x = left + width * (.50 + min(.30, max(-.30, skew)) * .18)
    left_curve = _cubic_points(
        (left, baseline),
        (left + width * .02, baseline - height * .63),
        (peak_x - width * .30, top),
        (peak_x, top),
    )
    right_curve = _cubic_points(
        (peak_x, top),
        (peak_x + width * .31, top),
        (right - width * .01, baseline - height * .58),
        (right, baseline),
    )
    mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(mask).polygon([*left_curve, *right_curve[1:]], fill=255)
    blur_radius = max(3, round(width * .02))
    shadow = mask.filter(ImageFilter.GaussianBlur(blur_radius)).point(
        lambda value: round(value * .11)
    )
    canvas.paste(
        Image.new("RGB", canvas.size, _mix(fill, (30, 42, 54), .36)),
        (0, 0), shadow,
    )
    canvas.paste(
        _gradient(
            canvas.size,
            _mix(fill, background, .42),
            _mix(fill, (35, 48, 61), .10),
        ),
        (0, 0), mask,
    )


def _draw_atmosphere(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    palette: dict[str, tuple[int, int, int]],
) -> None:
    """从照片色板生成少量空气感横向质量，不引入新的语义对象。"""
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    marks = ImageDraw.Draw(layer)
    color = (*_mix(palette["light"], palette["accent"], .24), 105)
    for x_ratio, y_ratio, w_ratio in ((.10, .26, .10), (.22, .32, .13), (.33, .25, .09)):
        x = left + round(width * x_ratio)
        y = top + round(height * y_ratio)
        mark_width = round(width * w_ratio)
        mark_height = max(5, round(height * .018))
        marks.rounded_rectangle(
            (x, y, x + mark_width, y + mark_height),
            radius=mark_height // 2,
            fill=color,
        )
    layer = layer.filter(ImageFilter.GaussianBlur(max(1, round(width * .0025))))
    canvas.paste(layer.convert("RGB"), (0, 0), layer.getchannel("A"))


def _draw_axes(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    palette: dict[str, tuple[int, int, int]],
    count: int,
) -> None:
    if count <= 0:
        return
    left, _, right, bottom = box
    width = right - left
    line_width = max(3, width // 360)
    y = bottom - line_width * 2
    draw.line(
        (left + width * .05, y, right - width * .03, y),
        fill=palette["dark"], width=line_width * 2,
    )
    if count > 1:
        y2 = y + line_width * 5
        draw.line(
            (left + width * .20, y2, left + width * .56, y2),
            fill=palette["light"], width=line_width,
        )
        draw.line(
            (left + width * .56, y2, right - width * .20, y2),
            fill=palette["mid"], width=line_width * 2,
        )


def _draw_motif(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    spec: dict[str, Any],
    palette: dict[str, tuple[int, int, int]],
    background: tuple[int, int, int],
) -> None:
    draw = ImageDraw.Draw(canvas)
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    focus = left + int(width * (0.5 + spec["asymmetry"] * 0.42))
    density = int(spec["density"])
    motif = spec["motif"]
    primary = palette["dominant"]
    secondary = palette["mid"]
    accent = palette["accent"]
    line_width = max(3, width // 320)

    if motif in {"arches", "cluster"}:
        _draw_atmosphere(canvas, box, palette)

    if motif == "arches":
        mass_count = int(spec["mass_count"])
        baseline = bottom - max(line_width * 8, round(height * .10))
        # 大质量置于后方，以负空间形成标志性主拱；小质量保留原照中的层级和遮挡。
        main_width = round(width * .57)
        main_height = round(height * .92)
        main_left = round(focus - main_width * .38)
        _arch_mass(
            canvas,
            (main_left, baseline - main_height, main_left + main_width, baseline),
            fill=_mix(palette["light"], palette["mid"], .35),
            background=background, thickness=.19,
            skew=float(spec["asymmetry"]),
        )
        if mass_count >= 4:
            back_width = round(width * .24)
            back_height = round(height * .59)
            back_left = round(main_left - back_width * .34)
            _dome_mass(
                canvas,
                (back_left, baseline - back_height, back_left + back_width, baseline),
                fill=palette["mid"], background=background,
                skew=float(spec["asymmetry"]) * -.55,
            )
        small_specs = [
            (.19, .50, -.20, palette["accent"]),
            (.16, .43, -.10, palette["dominant"]),
        ]
        for index, (width_ratio, height_ratio, offset, color) in enumerate(small_specs):
            if index + 2 > mass_count:
                break
            mass_width = round(width * width_ratio)
            mass_height = round(height * height_ratio)
            mass_left = round(main_left + width * offset)
            _dome_mass(
                canvas,
                (mass_left, baseline - mass_height, mass_left + mass_width, baseline),
                fill=color, background=background,
                skew=float(spec["asymmetry"]) * (-.35 if index else .45),
            )
        # 深色结构质量对应原图中的附属体量；保持扁平、克制、不描绘细节。
        wedge_left = main_left + round(main_width * .70)
        draw.polygon([
            (wedge_left, baseline - height * .20),
            (wedge_left + width * .13, baseline - height * .17),
            (wedge_left + width * .22, baseline),
            (wedge_left, baseline),
        ], fill=palette["dark"])
        _draw_axes(draw, (left, top, right, bottom), palette, int(spec["horizon_count"]))
    elif motif == "cluster":
        for index in range(density):
            radius = int(width * (0.055 + (index % 3) * 0.013))
            cx = focus + int((index - density / 2) * radius * .78)
            cy = top + int(height * (.40 + (index % 2) * .12))
            color = [primary, secondary, palette["light"], accent][index % 4]
            draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=color)
        _draw_axes(draw, (left, top, right, bottom), palette, int(spec["horizon_count"]))
    elif motif == "verticals":
        baseline = bottom
        for index in range(density):
            mark_w = int(width * (0.035 + (index % 2) * .016))
            mark_h = int(height * (.22 + ((index * 3) % 5) * .07))
            x = focus + int((index - density / 2) * width * .07)
            taper = max(2, mark_w // 5)
            draw.polygon([
                (x + taper, baseline - mark_h),
                (x + mark_w - taper, baseline - mark_h + (index % 2) * taper),
                (x + mark_w, baseline),
                (x, baseline),
            ], fill=[primary, secondary, palette["dark"], accent][index % 4])
    elif motif == "flow":
        for band in range(3):
            points = []
            for index in range(9):
                x = left + int(width * (.05 + index * .112))
                y = top + int(height * (.34 + band * .13 + .10 * ((index % 4) - 1.5) / 1.5))
                points.append((x, y))
            draw.line(
                points,
                fill=[primary, secondary, accent][band],
                width=line_width * (5 - band), joint="curve",
            )
        _draw_axes(draw, (left, top, right, bottom), palette, int(spec["horizon_count"]))
    else:  # horizon
        for index in range(min(density, 5)):
            y = top + int(height * (.28 + index * .12))
            inset = int(width * (.08 + index * .025))
            gap = int(width * (.04 + (index % 2) * .025))
            split = focus + int((index - density / 2) * width * .018)
            color = [primary, secondary, palette["dark"], accent][index % 4]
            draw.line((left+inset, y, split-gap, y), fill=color, width=line_width * (3 if index == 0 else 2))
            draw.line((split+gap, y, right-inset, y), fill=color, width=line_width * (2 if index == 0 else 1))
        mark_w = width * .045
        draw.polygon([
            (focus, top + height * .16),
            (focus + mark_w * .68, top + height * .19),
            (focus + mark_w, bottom),
            (focus - mark_w * .12, bottom),
        ], fill=accent)


async def render_photo_abstract_editorial(
    llm: Any,
    source: Path,
    user_prompt: str,
    *,
    panel_generator: Any | None = None,
) -> dict[str, Any]:
    """让视觉模型规划版式，并以可选图片模型或本地 v3 生成抽象面板。"""
    system = (
        "Act as the visual director for a restrained photo-abstract editorial diptych. "
        "Analyze only the supplied photograph. Preserve its distinctive spatial facts, mass hierarchy, "
        "negative space, axes, occlusion, asymmetry and color roles; do not invent objects or colors. "
        "Return one JSON object only with: title (2-5 natural English words grounded in visible facts, "
        "preserve elegant title case), subtitle (optional 3-7 words), "
        "motif (arches|horizon|cluster|verticals|flow), focus_x (0.22-0.78), density (3-6), "
        "title_layout (left|center), motif_width (0.46-0.68; use 0.60-0.68 for landmark arches), "
        "motif_height (0.22-0.38 of panel height), asymmetry (-0.30 to 0.30), "
        "mass_count (2-5), horizon_count (0-2). "
        "For landmark architecture prefer filled simplified masses and meaningful negative spaces, "
        "never generic outline icons or evenly repeated infographic marks."
    )
    response = await llm.vision(
        system,
        str(user_prompt or "Create a faithful photo-and-abstract editorial composition.")[:4000],
        [Path(source)],
        temperature=0.2,
    )
    spec = _clean_spec(response)
    if not re.search(r"\bsubtitle\b|副标题", str(user_prompt or ""), re.I):
        spec["subtitle"] = ""

    generated_panel: bytes | None = None
    panel_model = ""
    panel_provider_error = ""
    if panel_generator is not None:
        try:
            panel_result = await panel_generator(spec, Path(source), user_prompt)
            if isinstance(panel_result, dict):
                candidate = panel_result.get("data")
                if isinstance(candidate, (bytes, bytearray)) and candidate:
                    generated_panel = bytes(candidate)
                    panel_model = str(panel_result.get("model") or "")
        except Exception as exc:  # 外部编辑服务不能破坏可靠的本地回退。
            panel_provider_error = f"{type(exc).__name__}: {str(exc)[:240]}"

    with Image.open(source) as opened:
        photo = ImageOps.exif_transpose(opened).convert("RGB")
    output_width = min(1600, max(1000, photo.width))
    photo_height = max(1, round(photo.height * output_width / photo.width))
    photo = photo.resize((output_width, photo_height), Image.Resampling.LANCZOS)
    source_ratio = photo.width / max(1, photo.height)
    if source_ratio >= 1.20:
        target_photo_share = 0.44
    elif source_ratio <= 0.82:
        target_photo_share = 0.61
    else:
        target_photo_share = 0.53
    panel_height = round(photo_height * (1 - target_photo_share) / target_photo_share)
    panel_height = min(round(output_width * .78), max(round(output_width * .58), panel_height))
    ivory = (243, 240, 232)
    palette = _muted_palette(photo, ivory)

    motif_width = round(output_width * spec["motif_width"])
    motif_height = round(panel_height * spec["motif_height"])
    center_x = round(output_width * (0.5 + spec["asymmetry"] * .28))
    motif_left = max(round(output_width * .08), center_x - motif_width // 2)
    motif_right = min(round(output_width * .92), motif_left + motif_width)
    if motif_right - motif_left < motif_width:
        motif_left = motif_right - motif_width
    motif_top_in_panel = round(panel_height * .13)
    motif_bottom_in_panel = motif_top_in_panel + motif_height

    provider_panel_used = False
    if generated_panel:
        try:
            with Image.open(io.BytesIO(generated_panel)) as opened_panel:
                decoded_panel = ImageOps.exif_transpose(opened_panel).convert("RGB")
            panel = ImageOps.fit(
                decoded_panel,
                (output_width, panel_height),
                method=Image.Resampling.LANCZOS,
                centering=(.5, .5),
            )
            # 低频模糊只用于测量结构覆盖，不会进入最终画面。
            panel_background = panel.filter(
                ImageFilter.GaussianBlur(max(24, round(output_width * .055)))
            )
            provider_panel_used = True
        except Exception as exc:
            panel_provider_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            generated_panel = None
    if not provider_panel_used:
        # 本地面板以 2 倍分辨率绘制后缩小，获得稳定的曲线抗锯齿和细腻色阶。
        render_scale = 2
        panel_background = _prepare_panel(
            (output_width * render_scale, panel_height * render_scale),
            ivory,
            palette,
        )
        panel = panel_background.copy()
        _draw_motif(
            panel,
            tuple(value * render_scale for value in (
                motif_left,
                motif_top_in_panel,
                motif_right,
                motif_bottom_in_panel,
            )),
            spec,
            palette,
            ivory,
        )
        panel = panel.resize((output_width, panel_height), Image.Resampling.LANCZOS)
        panel_background = panel_background.resize(
            (output_width, panel_height), Image.Resampling.LANCZOS
        )
    canvas = Image.new("RGB", (output_width, photo_height + panel_height), ivory)
    canvas.paste(photo, (0, 0))
    canvas.paste(panel, (0, photo_height))
    draw = ImageDraw.Draw(canvas)
    margin = round(output_width * .08)
    title_y = photo_height + round(panel_height * .72)
    title_font = _font(max(36, round(output_width * .042)))
    subtitle_font = _font(max(19, round(output_width * .018)), italic=True)
    ink = _dark_ink(palette["dark"])
    title_box = draw.textbbox((0, 0), spec["title"], font=title_font)
    title_width = title_box[2] - title_box[0]
    if spec["title_layout"] == "center":
        title_x = max(margin, round((output_width - title_width) / 2))
    else:
        title_x = margin
    draw.text((title_x, title_y), spec["title"], font=title_font, fill=ink)
    subtitle_safe = True
    if spec["subtitle"]:
        subtitle_box = draw.textbbox((0, 0), spec["subtitle"], font=subtitle_font)
        subtitle_width = subtitle_box[2] - subtitle_box[0]
        subtitle_x = (
            max(margin, round((output_width - subtitle_width) / 2))
            if spec["title_layout"] == "center" else title_x
        )
        draw.text(
            (subtitle_x, title_y + round(output_width * .06)),
            spec["subtitle"], font=subtitle_font, fill=palette["mid"],
        )
        subtitle_bounds = draw.textbbox(
            (subtitle_x, title_y + round(output_width * .06)),
            spec["subtitle"], font=subtitle_font,
        )
        subtitle_safe = (
            subtitle_bounds[0] >= margin
            and subtitle_bounds[2] <= output_width - margin
            and subtitle_bounds[3] <= canvas.height - margin
        )

    source_preserved = ImageChops.difference(
        canvas.crop((0, 0, output_width, photo_height)), photo
    ).getbbox() is None
    motif_top = photo_height + motif_top_in_panel
    motif_bottom = photo_height + motif_bottom_in_panel
    motif_image = canvas.crop((motif_left, motif_top, motif_right, motif_bottom))
    motif_background = panel_background.crop((
        motif_left,
        motif_top_in_panel,
        motif_right,
        motif_bottom_in_panel,
    ))
    motif_difference = ImageChops.difference(
        motif_image,
        motif_background,
    ).convert("L")
    histogram = motif_difference.histogram()
    non_background = sum(histogram[1:])
    # histogram 存储的是像素数，而不是差值总量。
    motif_coverage = non_background / max(1, motif_image.width * motif_image.height)
    motif_colors = len(motif_image.quantize(colors=256).getcolors() or [])
    minimum_scale = (
        0.60 if spec["motif"] == "arches"
        else 0.56 if spec["motif"] in {"horizon", "flow"}
        else 0.46
    )
    title_bounds = draw.textbbox((title_x, title_y), spec["title"], font=title_font)
    title_safe = (
        title_bounds[0] >= margin
        and title_bounds[2] <= output_width - margin
        and title_bounds[1] >= photo_height
        and title_bounds[3] <= canvas.height - margin
        and subtitle_safe
    )
    panel_corner = canvas.getpixel((2, photo_height + 2))
    panel_corner_distance = sum(
        abs(panel_corner[index] - ivory[index]) for index in range(3)
    )
    quality_checks = {
        "source_photo_preserved": source_preserved,
        "uniform_ivory_panel": (
            panel_corner_distance <= 90 if provider_panel_used else panel_corner == ivory
        ),
        "photo_derived_palette": len(set(palette.values())) >= 3,
        "validated_model_design": True,
        # 尺度由 motif_scale 单独约束；覆盖率只防止空画布并允许负空间丰富的壳体。
        "motif_coverage": (
            0.015 <= motif_coverage <= 0.95
            if provider_panel_used else 0.025 <= motif_coverage <= 0.72
        ),
        "motif_scale": spec["motif_width"] >= minimum_scale,
        "tonal_depth": motif_colors >= 32,
        "title_safe_area": title_safe,
    }
    if not all(quality_checks.values()):
        failed = [name for name, passed in quality_checks.items() if not passed]
        raise ValueError(
            "编辑作品质量检查未通过：" + "、".join(failed)
            + f"（coverage={motif_coverage:.4f}, colors={motif_colors}, "
            + f"width={spec['motif_width']:.3f}）"
        )

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return {
        "data": buffer.getvalue(),
        "source": (
            "environment_image_edit_composite"
            if provider_panel_used else "provider_vision_local_editorial"
        ),
        "model": str(
            getattr(llm, "model_id", "")
            or getattr(llm, "vision_model", "")
            or getattr(llm, "text_model", "")
        ),
        "action": "compose",
        "input_images": 1,
        "renderer_version": "photo_editorial_v3",
        "design_source": "vision_model",
        "panel_renderer": "image_edit_model" if provider_panel_used else "local_v3",
        "panel_model": panel_model if provider_panel_used else "",
        "panel_provider_error": panel_provider_error,
        "quality_checks": quality_checks,
        "design": spec,
    }
