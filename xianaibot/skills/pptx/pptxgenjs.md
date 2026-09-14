# PptxGenJS Tutorial

## End-to-End Workflow

Follow these steps in order.

1. **Design** — choose style, color palette, typography, plan layouts (see [SKILL.md — Style Discovery](SKILL.md#style-discovery) and [Design Ideas](SKILL.md#design-ideas))
2. **(Optional) Acquire images** — if the design calls for images, use ImageGen (see [ImageGen](#imagegen) below)
3. **Build slides** — write PptxGenJS code and generate .pptx (API reference below)
4. **QA — automated checks**:
   - `python3 scripts/fix_pptx.py output.pptx`
   - `python3 scripts/validate_layout.py output.pptx`
   - `python3 -m markitdown output.pptx`
5. **Fix and re-verify** — fix all issues → regenerate → re-run QA until clean
6. **Deliver** — only after a full QA pass with zero issues

---

## Setup & Basic Structure

```javascript
const pptxgen = require("pptxgenjs");

let pres = new pptxgen();
pres.author = 'Your Name';
pres.title = 'Presentation Title';

// ============================================================
// ⚠️ SLIDE DIMENSIONS - REQUIRED FIRST, USE EVERYWHERE
// ============================================================
// Choose ONE layout and define its dimensions:
pres.layout = 'LAYOUT_16x9';
const SLIDE_W = 10;      // inches
const SLIDE_H = 5.625;   // inches

// Define safe content area (with margins)
const MARGIN = 0.5;
const CONTENT_X = MARGIN;
const CONTENT_Y = MARGIN;
const CONTENT_W = SLIDE_W - (2 * MARGIN);  // 9 inches
const CONTENT_H = SLIDE_H - (2 * MARGIN);  // 4.625 inches

// Common layout helpers
const CENTER_X = SLIDE_W / 2;              // 5 inches
const CENTER_Y = SLIDE_H / 2;              // 2.8125 inches
// ============================================================

// ============================================================
// ⚠️ CONTAINER SYSTEM - REQUIRED IN EVERY SCRIPT
// ============================================================

// Image scaling helper - uses pptxgenjs native sizing to fit/fill target bounds
// mode: 'contain' (fit inside, may leave space) or 'cover' (fill area, may crop)
function calculateScaledImageOpts(opts) {
  const { path, w: targetW, h: targetH, x = 0, y = 0, mode = 'cover', ...rest } = opts;
  if (!path || !targetW || !targetH) return opts;

  return {
    path,
    x,
    y,
    w: targetW,
    h: targetH,
    sizing: { type: mode, w: targetW, h: targetH },
    ...rest
  };
}

function createVirtualNode(type, data, parentX = 0, parentY = 0) {
  const opts = data.opts || {};
  const node = {
    type, data,
    absX: parentX + (opts.x || 0),
    absY: parentY + (opts.y || 0),
    w: opts.w || 0, h: opts.h || 0,
    children: []
  };
  node.addShape = function(shapeType, opts = {}) {
    const child = createVirtualNode('shape', { shapeType, opts }, node.absX, node.absY);
    node.children.push(child);
    return child;
  };
  node.addText = function(text, opts = {}) {
    const safeOpts = { fit: "shrink", ...opts };
    const bulletRe = /^(?:[\u2022\u2023\u25E6\u2043\u2219\u00B7\u25CF\u25CB\u2013\u2014]\s*|\-\s+)/;
    if (Array.isArray(text)) {
      text = text.map(item => {
        if (item && item.options && item.options.bullet && typeof item.text === 'string') {
          return { ...item, text: item.text.replace(bulletRe, '') };
        }
        return item;
      });
    }
    const child = createVirtualNode('text', { text, opts: safeOpts }, node.absX, node.absY);
    node.children.push(child);
    return child;
  };
  node.addImage = function(opts = {}) {
    const scaledOpts = calculateScaledImageOpts(opts);
    const child = createVirtualNode('image', { opts: scaledOpts }, node.absX, node.absY);
    node.children.push(child);
    return child;
  };
  node.addTable = function(tableData, opts = {}) {
    const child = createVirtualNode('table', { tableData, opts }, node.absX, node.absY);
    node.children.push(child);
    return child;
  };
  return node;
}

function flattenNode(node, realSlide, pres) {
  const absOpts = { ...node.data.opts, x: node.absX, y: node.absY };
  if (node.type === 'shape') realSlide.addShape(node.data.shapeType, absOpts);
  else if (node.type === 'text') realSlide.addText(node.data.text, absOpts);
  else if (node.type === 'image') realSlide.addImage(absOpts);
  else if (node.type === 'table') realSlide.addTable(node.data.tableData, absOpts);
  node.children.forEach(child => flattenNode(child, realSlide, pres));
}

const originalAddSlide = pres.addSlide.bind(pres);
pres.addSlide = function(options) {
  const realSlide = originalAddSlide(options);
  const virtualSlide = {
    children: [],
    _realSlide: realSlide,
    set background(val) { realSlide.background = val; },
    get background() { return realSlide.background; },
    addShape: function(shapeType, opts = {}) {
      const node = createVirtualNode('shape', { shapeType, opts }, 0, 0);
      this.children.push(node);
      return node;
    },
    addText: function(text, opts = {}) {
      const safeOpts = { fit: "shrink", ...opts };
      const node = createVirtualNode('text', { text, opts: safeOpts }, 0, 0);
      this.children.push(node);
      return node;
    },
    addImage: function(opts = {}) {
      const scaledOpts = calculateScaledImageOpts(opts);
      const node = createVirtualNode('image', { opts: scaledOpts }, 0, 0);
      this.children.push(node);
      return node;
    },
    addTable: function(tableData, opts = {}) {
      const node = createVirtualNode('table', { tableData, opts }, 0, 0);
      this.children.push(node);
      return node;
    },
    addChart: function(chartType, data, opts = {}) {
      realSlide.addChart(chartType, data, opts);
    },
    render: function() {
      this.children.forEach(child => flattenNode(child, realSlide, pres));
    }
  };
  return virtualSlide;
};
// ============================================================

let slide = pres.addSlide();
slide.addText("Hello World!", { x: 0.5, y: 0.5, fontSize: 36, color: "363636" });
slide.render();

pres.writeFile({ fileName: "Presentation.pptx" });
```

## Required Slide Structure (From-Scratch Only)

When creating a presentation from scratch, you MUST follow this structure:

1. Slide 1: Cover page
2. Slide 2: Table of contents (agenda)
3. Slide 3 onward: Content sections in the exact order listed on Slide 2
4. Final slide: Closing/thank-you page

Do not skip the table of contents slide. Do not place content slides before the table of contents.

### ⚠️ No Blank Slides Allowed

Every slide MUST contain at least one visible element (`addText`, `addShape`, `addImage`, `addChart`, or `addTable`) inside the slide. A slide with only a background color and no content elements is a generation defect.

Common causes of blank slides:
- Forgetting to add content to section divider / transition slides
- Token truncation mid-generation leaving some slides empty
- Assuming slide layout or master will auto-fill content (they won't — pptxgenjs creates minimal layouts with no inherited shapes)

If you intend a slide as a section divider, you MUST explicitly add elements — e.g., a large section number, title text, and/or decorative shapes.

---

## How to Use Containers

```javascript
// Create a card as a container
let card = slide.addShape(pres.shapes.RECTANGLE, {
  x: 1, y: 2, w: 4, h: 2.5,
  fill: { color: "FFFFFF" }
});

// Add content INSIDE the card - coordinates relative to card's top-left
card.addText("Title", { x: 0.2, y: 0.2, w: 3.6, h: 0.4, fontSize: 18 });
card.addText("Description", { x: 0.2, y: 0.7, w: 3.6, h: 1.5, fontSize: 12 });

// Nested containers work too
let iconBg = card.addShape(pres.shapes.OVAL, { x: 0.2, y: 1.8, w: 0.5, h: 0.5 });
iconBg.addText("✓", { x: 0, y: 0, w: 0.5, h: 0.5, align: "center" });

// ⚠️ REQUIRED: Call render() at end of each slide
slide.render();
```

**For multi-line text that should wrap**, explicitly disable overflow protection:
```javascript
slide.addText("Long paragraph...", {
  x: 1, y: 2, w: 8, h: 3,
  autoFit: false,  // Override the default
  fit: "none"      // Allow normal wrapping
});
```

### How to Use Images (Aspect Ratio Preserved)

**CRITICAL: The target `w` and `h` MUST match the aspect ratio of the generated image.** Use the `image_size` enum to determine the correct ratio:

| `image_size` enum | Aspect Ratio | Example target (w × h) |
|-------------------|-------------|------------------------|
| `landscape_16_9` | 16:9 | `w: 5.33, h: 3.0` or `w: 8, h: 4.5` |
| `landscape_4_3` | 4:3 | `w: 4.0, h: 3.0` or `w: 5.33, h: 4.0` |
| `square` | 1:1 | `w: 3.0, h: 3.0` or `w: 4, h: 4` |
| `portrait_4_3` | 3:4 | `w: 3.0, h: 4.0` |
| `portrait_16_9` | 9:16 | `w: 2.5, h: 4.44` |

**Rules:**
1. Target `w` and `h` MUST have the same ratio as the image (e.g., 4:3 image → target must be 4:3)
2. Use `mode: 'cover'` (default) for content images — fills the area completely with slight crop if needed
3. For full-slide backgrounds, use `slide.background = { path: "..." }` directly (no sizing needed)

```javascript
// landscape_4_3 image (4:3) placed in a matching 4:3 target area
slide.addImage(calculateScaledImageOpts({
  path: "images/solution.png",
  x: 0.5, y: 1.5,
  w: 4.0, h: 3.0   // 4:3 ratio matches the image
}));
```

```javascript
// landscape_16_9 image (16:9) as a wide hero banner
slide.addImage(calculateScaledImageOpts({
  path: "images/hero.png",
  x: 0.5, y: 1.0,
  w: 9.0, h: 5.0,  // ~16:9 ratio matches the image
  mode: 'cover'     // default - fills area completely
}));
```

```javascript
// Inside a card - use matching ratio for the image area
let card = slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.5, y: 1, w: 5, h: 4 });
card.addImage(calculateScaledImageOpts({
  path: "images/chart.png",  // generated with landscape_4_3
  x: 0.2, y: 0.2,
  w: 4.6, h: 3.45  // 4:3 ratio (4.6 / 3.45 ≈ 1.33)
}));
```

---

## Card System

All information blocks should be placed inside cards. Bare text floating directly on the slide lacks structure and polish.

### Standard Card Parameters

| Property | Recommended Value | Notes |
|----------|-------------------|-------|
| shape | ROUNDED_RECTANGLE | Never use sharp RECTANGLE for content cards |
| rectRadius | 0.1 | Consistent rounding across all cards |
| fill | `{ color: "FFFFFF" }` or `{ color: "1F2937" }` | Light/dark depends on slide background |
| shadow | `makeShadow()` | Always add shadow for depth |
| padding | 0.25" (all sides) | Child elements start at x+0.25, y+0.25 |

### Shadow Factory (required)

Always create shadow objects via a factory function to avoid shared-reference bugs (see Common Pitfalls #11):

```javascript
const makeShadow = () => ({
  type: "outer", blur: 12, offset: 4, angle: 135,
  color: "000000", opacity: 0.28
});

// Emphasized shadow (for hero/featured cards)
const makeHeroShadow = () => ({
  type: "outer", blur: 16, offset: 6, angle: 135,
  color: "000000", opacity: 0.32
});
```

### Card Grid Layout Helper

```javascript
// Generate equally-sized card positions in a grid
function cardGrid(cols, rows, opts = {}) {
  const { startX = CONTENT_X, startY = 1.4, areaW = CONTENT_W, areaH = CONTENT_H - 0.9,
          gapX = 0.25, gapY = 0.25, padding = 0.25 } = opts;
  const cardW = (areaW - gapX * (cols - 1)) / cols;
  const cardH = (areaH - gapY * (rows - 1)) / rows;
  const cards = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      cards.push({
        x: startX + c * (cardW + gapX),
        y: startY + r * (cardH + gapY),
        w: cardW, h: cardH, padding
      });
    }
  }
  return { cards, cardW, cardH };
}
```

### Usage Example

```javascript
const { cards } = cardGrid(3, 2); // 3-column, 2-row grid
cards.forEach((pos, i) => {
  let card = slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: pos.x, y: pos.y, w: pos.w, h: pos.h,
    fill: { color: "FFFFFF" }, rectRadius: 0.1, shadow: makeShadow()
  });
  card.addText(titles[i], {
    x: pos.padding, y: pos.padding,
    w: pos.w - pos.padding * 2, h: 0.4,
    fontSize: 16, bold: true, color: "1F2329"
  });
  card.addText(descriptions[i], {
    x: pos.padding, y: 0.75,
    w: pos.w - pos.padding * 2, h: pos.h - 1.1,
    fontSize: 13, color: "6B7280"
  });
});
slide.render();
```

### Card Design Principles

- **Every independent content block goes in a card** — no bare text on the slide surface
- **Cards on the same slide must be uniform in size** — maintain grid alignment
- **Intra-card text hierarchy**: title bold 16-18pt + description regular 12-14pt
- **Consistent gap between cards**: 0.2-0.3"
- **One accent color per card set** — use for card header icon or top border, not the whole fill
- **Prefer image cards over text-only cards** — see Image Card below

### Image Card (card with embedded image)

Cards with an image on top and text below create richer visuals than text-only cards. When a slide has 2-3 cards, at least one should be an image card.

```javascript
const { cards } = cardGrid(3, 1, { startY: 1.4, areaH: 4.0 });
cards.forEach((pos, i) => {
  let card = slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: pos.x, y: pos.y, w: pos.w, h: pos.h,
    fill: { color: "FFFFFF" }, rectRadius: 0.1, shadow: makeShadow()
  });
  // Image fills top ~55% of card
  const imgH = pos.h * 0.55;
  card.addImage(calculateScaledImageOpts({
    path: images[i], x: 0.1, y: 0.1, w: pos.w - 0.2, h: imgH - 0.1
  }));
  // Title below image
  card.addText(titles[i], {
    x: pos.padding, y: imgH + 0.15,
    w: pos.w - pos.padding * 2, h: 0.35,
    fontSize: 15, bold: true, color: "1F2329"
  });
  // Description
  card.addText(descriptions[i], {
    x: pos.padding, y: imgH + 0.55,
    w: pos.w - pos.padding * 2, h: pos.h - imgH - 0.7,
    fontSize: 12, color: "6B7280"
  });
});
slide.render();
```

---

## Layout Dimensions

Slide dimensions (coordinates in inches):
- `LAYOUT_16x9`: 10" × 5.625" (default)
- `LAYOUT_16x10`: 10" × 6.25"
- `LAYOUT_4x3`: 10" × 7.5"
- `LAYOUT_WIDE`: 13.3" × 7.5"

---

## Layout Safety (Prevent Overflow Before QA)

Do layout checks in code before rendering. QA should validate, not discover basic bounds errors.

### 1) Pick layout from required canvas

If your lowest element has `y + h = 6.6`, `LAYOUT_16x9` will overflow. Choose a 7.5" high layout (`LAYOUT_4x3` or `LAYOUT_WIDE`) or compress the layout.

### 2) Reserve margins and content frame first

```javascript
const LAYOUT_SIZE = {
  LAYOUT_16x9: { w: 10, h: 5.625 },
  LAYOUT_16x10: { w: 10, h: 6.25 },
  LAYOUT_4x3: { w: 10, h: 7.5 },
  LAYOUT_WIDE: { w: 13.3, h: 7.5 }
};

const MARGIN = { left: 0.5, right: 0.5, top: 0.5, bottom: 0.5 };

function getContentFrame(layout) {
  const size = LAYOUT_SIZE[layout];
  return {
    slideW: size.w,
    slideH: size.h,
    x: MARGIN.left,
    y: MARGIN.top,
    w: size.w - MARGIN.left - MARGIN.right,
    h: size.h - MARGIN.top - MARGIN.bottom
  };
}
```

### 3) Guard every element by boundary rule

```javascript
function assertInBounds(box, frame, id) {
  const right = frame.slideW - MARGIN.right;
  const bottom = frame.slideH - MARGIN.bottom;
  if (box.x < MARGIN.left || box.y < MARGIN.top || box.x + box.w > right || box.y + box.h > bottom) {
    throw new Error(`Out of bounds: ${id} (${box.x}, ${box.y}, ${box.w}, ${box.h})`);
  }
}
```

Before each `addText` / `addShape` / `addImage` / `addChart`, run `assertInBounds`.

### 4) Use stack layout for vertical blocks

Avoid hardcoded random `y` values. Stack sections with consistent gap:

```javascript
function stackY(startY, blocks, gap) {
  const positions = [];
  let y = startY;
  for (const h of blocks) {
    positions.push(y);
    y += h + gap;
  }
  return positions;
}
```

For text-heavy slides, use `gap >= 0.3` and keep at least `0.4` unused space at the bottom.

### 5) Scale coordinates when switching layouts

```javascript
function scaleBox(box, from, to) {
  return {
    x: box.x * (to.w / from.w),
    y: box.y * (to.h / from.h),
    w: box.w * (to.w / from.w),
    h: box.h * (to.h / from.h)
  };
}
```

This prevents partial migration errors when moving from `LAYOUT_WIDE`/`4x3` coordinates to `16x9`.

### 6) Prevent Overlap

 Use a simple collision detector for dynamic layouts (like dashboards or generated content).

 ```javascript
 const placedBoxes = [];

 function addBox(id, x, y, w, h) {
   const newBox = { id, x, y, w, h, r: x + w, b: y + h };

   for (const box of placedBoxes) {
     if (x < box.r && x + w > box.x && y < box.b && y + h > box.y) {
       console.warn(`OVERLAP DETECTED: ${id} overlaps with ${box.id}`);
       // Strategy: Shift down or resize
       y = box.b + 0.2;
     }
   }

   placedBoxes.push({ id, x, y, w, h, r: x + w, b: y + h });
   return { x, y };
 }
 ```

 ---

 ## Text & Formatting

**Title fonts MUST be serif.** Use a serif font (e.g., Georgia, Cambria, Palatino Linotype) for cover titles, slide titles, and subtitles. Body text should use a clean sans-serif font (e.g., Calibri, Arial). Only fall back to sans-serif for titles if no serif font is available.

**Serif charSpacing rule** — apply based on font size: ≥36pt → 2.5, 24-35pt → 1.5, 18-23pt → 1, 12-17pt → 0.5, <12pt → 0.

```javascript
// Cover title (44pt → charSpacing 2.5)
slide.addText("Presentation Title", {
  x: 1, y: 2, w: 8, h: 2, fontSize: 44, fontFace: "Georgia",
  color: "363636", bold: true, align: "center", valign: "middle",
  charSpacing: 2.5
});

// Slide title (32pt → charSpacing 1.5)
slide.addText("Slide Title", {
  x: 0.5, y: 0.3, w: 9, h: 0.8, fontSize: 32, fontFace: "Georgia",
  color: "363636", bold: true, charSpacing: 1.5
});

// Subtitle (20pt → charSpacing 1)
slide.addText("A brief subtitle here", {
  x: 0.5, y: 1.2, w: 9, h: 0.6, fontSize: 20, fontFace: "Georgia",
  color: "666666", charSpacing: 1
});

// Body text — use sans-serif font
slide.addText("Body content here", {
  x: 1, y: 4, w: 8, h: 1, fontSize: 16, fontFace: "Calibri",
  color: "363636"
});

// Rich text arrays
slide.addText([
  { text: "Bold ", options: { bold: true } },
  { text: "Italic ", options: { italic: true } }
], { x: 1, y: 3, w: 8, h: 1 });

// Multi-line text (requires breakLine: true)
slide.addText([
  { text: "Line 1", options: { breakLine: true } },
  { text: "Line 2", options: { breakLine: true } },
  { text: "Line 3" }  // Last item doesn't need breakLine
], { x: 0.5, y: 0.5, w: 8, h: 2 });

// Text box margin (internal padding)
slide.addText("Title", {
  x: 0.5, y: 0.3, w: 9, h: 0.6,
  margin: 0  // Use 0 when aligning text with other elements like shapes or icons
});
```

**Tip:** Text boxes have internal margin by default. Set `margin: 0` when you need text to align precisely with shapes, lines, or icons at the same x-position.

### Contrast Checker (WCAG)

Use this helper to validate text/background pairs before assigning colors:

```javascript
function hexToRgb(hex) {
  const clean = hex.replace("#", "");
  const full = clean.length === 3 ? clean.split("").map((c) => c + c).join("") : clean;
  return {
    r: parseInt(full.slice(0, 2), 16) / 255,
    g: parseInt(full.slice(2, 4), 16) / 255,
    b: parseInt(full.slice(4, 6), 16) / 255
  };
}

function linearize(v) {
  return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
}

function luminance(hex) {
  const { r, g, b } = hexToRgb(hex);
  return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b);
}

function contrastRatio(fgHex, bgHex) {
  const l1 = luminance(fgHex);
  const l2 = luminance(bgHex);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

function isContrastSafe(fgHex, bgHex, isLargeText = false) {
  const ratio = contrastRatio(fgHex, bgHex);
  return ratio >= (isLargeText ? 3 : 4.5);
}

const textColor = "F8FAFC";
const bgColor = "1F2937";
if (!isContrastSafe(textColor, bgColor, false)) {
  throw new Error(`Low contrast: ${textColor} on ${bgColor}`);
}
```

---

## Lists & Bullets

```javascript
// ✅ CORRECT: Multiple bullets
slide.addText([
  { text: "First item", options: { bullet: true, breakLine: true } },
  { text: "Second item", options: { bullet: true, breakLine: true } },
  { text: "Third item", options: { bullet: true } }
], { x: 0.5, y: 0.5, w: 8, h: 3 });

// ❌ WRONG: Never use unicode bullets
slide.addText("• First item", { ... });  // Creates double bullets

// Sub-items and numbered lists
{ text: "Sub-item", options: { bullet: true, indentLevel: 1 } }
{ text: "First", options: { bullet: { type: "number" }, breakLine: true } }
```

---

## Shapes

```javascript
slide.addShape(pres.shapes.RECTANGLE, {
  x: 0.5, y: 0.8, w: 1.5, h: 3.0,
  fill: { color: "FF0000" }, line: { color: "000000", width: 2 }
});

slide.addShape(pres.shapes.OVAL, { x: 4, y: 1, w: 2, h: 2, fill: { color: "0000FF" } });

slide.addShape(pres.shapes.LINE, {
  x: 1, y: 3, w: 5, h: 0, line: { color: "FF0000", width: 3, dashType: "dash" }
});

// With transparency
slide.addShape(pres.shapes.RECTANGLE, {
  x: 1, y: 1, w: 3, h: 2,
  fill: { color: "0088CC", transparency: 50 }
});
```

**Note**: Gradient fills are not natively supported. Use a gradient image as a background instead.

### Shadow Options

| Property | Type | Range | Notes |
|----------|------|-------|-------|
| `type` | string | `"outer"`, `"inner"` | |
| `color` | string | 6-char hex (e.g. `"000000"`) | No `#` prefix, no 8-char hex — see Common Pitfalls |
| `blur` | number | 0-100 pt | |
| `offset` | number | 0-200 pt | **Must be non-negative** — negative values corrupt the file |
| `angle` | number | 0-359 degrees | Direction the shadow falls (135 = bottom-right, 270 = upward) |
| `opacity` | number | 0.0-1.0 | Use this for transparency, never encode in color string |

To cast a shadow upward (e.g. on a footer bar), use `angle: 270` with a positive offset — do **not** use a negative offset.

### Shadow Design Guidelines

**When to use shadows:**
- ✅ Content cards (mandatory)
- ✅ Image containers
- ✅ Button/tag shapes
- ❌ Full-width background shapes (footers, sidebars)
- ❌ Decorative lines
- ❌ Text boxes themselves (put text inside a card with shadow)

**Standard shadow parameters by context:**

| Context | blur | offset | angle | opacity | Effect |
|---------|------|--------|-------|---------|--------|
| Content card | 12 | 4 | 135 | 0.28 | Visible float with depth |
| Hero/featured card | 16 | 6 | 135 | 0.32 | Strong elevation |
| Bottom bar (upward) | 6 | 3 | 270 | 0.15 | Subtle upward projection |

**Always use factory functions** — never share shadow object references across shapes (see Common Pitfalls #11):

```javascript
const makeShadow = () => ({
  type: "outer", blur: 12, offset: 4, angle: 135,
  color: "000000", opacity: 0.28
});

const makeHeroShadow = () => ({
  type: "outer", blur: 16, offset: 6, angle: 135,
  color: "000000", opacity: 0.32
});
```

### Building Diagrams with Native Shapes

Architecture diagrams, flowcharts, and process diagrams MUST use native shapes — never matplotlib or external image generators.

**Design guidelines:**
- Add `shadow` to nodes for depth — use `makeShadow()` from Card System section
- Use a single color family with varying lightness for nodes (e.g., dark → medium → light)
- Keep connector lines thinner (1.5pt) and lighter than node fills (e.g., gray)
- Center the diagram horizontally: `startX = (10 - totalWidth) / 2`
- Maintain equal spacing between nodes
- Use ROUNDED_RECTANGLE (rectRadius: 0.1-0.15) instead of sharp RECTANGLE
- White bold text on dark fills for readability

**Example 1 — Vertical Architecture Diagram:**

```javascript
const layers = [
  { label: "Presentation Layer", color: "065A82" },
  { label: "Business Logic",     color: "1C7293" },
  { label: "Data Access Layer",  color: "21295C" },
];
const nodeW = 4, nodeH = 0.8, gap = 1.2;
const startX = (10 - nodeW) / 2, startY = 1.2;
const nodeShadow = { type: "outer", blur: 12, offset: 4, color: "000000", opacity: 0.28 };

layers.forEach((layer, i) => {
  const y = startY + i * gap;
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: startX, y, w: nodeW, h: nodeH,
    fill: { color: layer.color }, rectRadius: 0.1, shadow: nodeShadow
  });
  slide.addText(layer.label, {
    x: startX, y, w: nodeW, h: nodeH,
    align: "center", valign: "middle",
    color: "FFFFFF", fontSize: 14, bold: true
  });
  if (i < layers.length - 1) {
    slide.addShape(pres.shapes.LINE, {
      x: startX + nodeW / 2, y: y + nodeH,
      w: 0, h: gap - nodeH,
      line: { color: "999999", width: 1.5, endArrowType: "triangle" }
    });
  }
});
```

**Example 2 — Horizontal Flowchart:**

```javascript
const steps = ["Plan", "Design", "Build", "Deploy"];
const colors = ["065A82", "1C7293", "21295C", "0A4D68"];
const nodeW = 1.8, nodeH = 0.9, hGap = 0.6;
const totalW = steps.length * nodeW + (steps.length - 1) * hGap;
const startX = (10 - totalW) / 2, nodeY = 2.2;
const nodeShadow = { type: "outer", blur: 12, offset: 4, color: "000000", opacity: 0.28 };

steps.forEach((label, i) => {
  const x = startX + i * (nodeW + hGap);
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x, y: nodeY, w: nodeW, h: nodeH,
    fill: { color: colors[i] }, rectRadius: 0.15, shadow: nodeShadow
  });
  slide.addText(label, {
    x, y: nodeY, w: nodeW, h: nodeH,
    align: "center", valign: "middle",
    color: "FFFFFF", fontSize: 13, bold: true
  });
  if (i < steps.length - 1) {
    slide.addShape(pres.shapes.LINE, {
      x: x + nodeW, y: nodeY + nodeH / 2,
      w: hGap, h: 0,
      line: { color: "999999", width: 1.5, endArrowType: "triangle" }
    });
  }
});
```

---

## External Assets

### ImageGen

Use ImageGen to generate slide illustrations and backgrounds directly — do not use WebSearch to find images online (search results are unreliable and often low-quality).

**Workflow:**
1. Decide which slides need images based on chosen style (see [SKILL.md — Style Discovery](SKILL.md#style-discovery))
2. For each, note: descriptive prompt, purpose (background/hero/illustration), aspect ratio
3. Generate with ImageGen using `image_size` enum:
   - `landscape_16_9` for 16:9 (full-slide backgrounds, wide layouts)
   - `landscape_4_3` for 4:3 (content area illustrations)
   - `square` for 1:1 (headshots, profile images, square icons)
   - `portrait_4_3` for 3:4 portrait orientation
   - `portrait_16_9` for 9:16 tall portrait orientation
4. Save as `image-01.png`, `image-02.png`, etc. in `<output-dir>/images/`

**Prompt Tips:**
- Be specific about style: "a modern flat illustration of…" or "a photorealistic aerial view of…"
- Include color guidance: "using warm tones of terracotta and sage green" to match the slide palette
- Describe composition: "left side shows X, right side shows Y, with negative space in the center for text overlay"
- Specify mood: "professional, clean, and minimal" vs "vibrant, energetic, and bold"
- **NEVER include text or letters in generated images** — especially for backgrounds. Generated text is always garbled and unreadable. Use PptxGenJS text elements instead. Explicitly add "no text, no letters, no words, no numbers" to your prompt when generating background images.

### Matplotlib/Python Charts

Define color palette at the start to match your PPT theme:

```python
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

# ============================================================
# COLOR PALETTE - Match your PPT theme
# ============================================================
# Example: "Ocean Gradient" theme
COLORS = {
    'primary': '#065A82',
    'secondary': '#1C7293',
    'accent': '#21295C',
    'background': '#F0F7FA',
    'text': '#21295C',
    'light': '#E8F4F8',
}

# Color cycle for multiple data series
COLOR_CYCLE = [COLORS['primary'], COLORS['secondary'], COLORS['accent'], '#4A90A4', '#2D5A6B']

# Apply theme globally
plt.rcParams.update({
    'axes.prop_cycle': plt.cycler(color=COLOR_CYCLE),
    'axes.facecolor': 'none',
    'axes.edgecolor': COLORS['text'],
    'axes.labelcolor': COLORS['text'],
    'text.color': COLORS['text'],
    'xtick.color': COLORS['text'],
    'ytick.color': COLORS['text'],
    'figure.facecolor': 'none',
    'font.family': 'sans-serif',
    'font.size': 12,
})
# ============================================================

# Create chart with theme colors
fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
bars = ax.bar(['Q1', 'Q2', 'Q3', 'Q4'], [25, 40, 35, 50],
              color=[COLORS['primary'], COLORS['secondary'], COLORS['primary'], COLORS['secondary']],
              edgecolor=COLORS['accent'], linewidth=1.5)
ax.set_title('Quarterly Revenue', fontsize=16, fontweight='bold', color=COLORS['text'])
ax.set_ylabel('Revenue ($M)', color=COLORS['text'])
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

plt.tight_layout()
width_px, height_px = int(8 * 150), int(5 * 150)
plt.savefig(f'chart_{width_px}x{height_px}.png', dpi=150, transparent=True)
plt.close()
```

**Matplotlib palette templates:**

```python
# Midnight Executive
COLORS = {'primary': '#1E2761', 'secondary': '#CADCFC', 'accent': '#FFFFFF', 'text': '#1E2761'}

# Forest & Moss
COLORS = {'primary': '#2C5F2D', 'secondary': '#97BC62', 'accent': '#F5F5F5', 'text': '#2C5F2D'}

# Coral Energy
COLORS = {'primary': '#F96167', 'secondary': '#F9E795', 'accent': '#2F3C7E', 'text': '#2F3C7E'}

# Warm Terracotta
COLORS = {'primary': '#B85042', 'secondary': '#E7E8D1', 'accent': '#A7BEAE', 'text': '#B85042'}

# Teal Trust
COLORS = {'primary': '#028090', 'secondary': '#00A896', 'accent': '#02C39A', 'text': '#028090'}
```

Use for:
- Complex statistical charts (bar, line, pie, scatter, radar, etc.)
- Custom visualizations not supported by pptxgenjs
- Heatmaps, scatter plots, box plots

⚠️ Do NOT use matplotlib for:
- Architecture diagrams, flowcharts, process diagrams, org charts, hierarchy diagrams, network topology diagrams
- Any diagram that primarily shows relationships between components

These MUST be built with pptxgenjs native shapes so they render as editable PPT objects — not raster images.

**matplotlib - Charts with CJK Support**

When creating charts/graphs with matplotlib that include Chinese text, you MUST configure the font:

```python
import os
import platform
import matplotlib.pyplot as plt
import matplotlib

def setup_matplotlib_cjk():
    """Configure matplotlib to support CJK characters"""
    system = platform.system()

    if system == "Darwin":  # macOS
        font_names = ['Arial Unicode MS', 'PingFang SC', 'Heiti SC', 'STHeiti']
    elif system == "Windows":
        font_names = ['Microsoft YaHei', 'SimHei', 'SimSun']
    else:  # Linux
        font_names = ['Noto Sans CJK SC', 'WenQuanYi Zen Hei', 'Droid Sans Fallback']

    # Find available font
    available_fonts = [f.name for f in matplotlib.font_manager.fontManager.ttflist]
    for font_name in font_names:
        if font_name in available_fonts:
            plt.rcParams['font.sans-serif'] = [font_name] + plt.rcParams['font.sans-serif']
            plt.rcParams['axes.unicode_minus'] = False  # Fix minus sign display
            return font_name
    return None

# Setup CJK font before creating charts
cjk_font = setup_matplotlib_cjk()
```

### Mermaid Diagrams (Last Resort)

Use only when the diagram cannot be represented cleanly with native shapes/layout, and keep it consistent with the deck:

```bash
# Install if needed: npm install -g @mermaid-js/mermaid-cli
```

Create a theme config file matching your PPT palette:

```javascript
// mermaid-config.json - Example for "Ocean Gradient" theme
{
  "theme": "base",
  "themeVariables": {
    "primaryColor": "#065A82",
    "primaryTextColor": "#FFFFFF",
    "primaryBorderColor": "#21295C",
    "secondaryColor": "#1C7293",
    "secondaryTextColor": "#FFFFFF",
    "secondaryBorderColor": "#065A82",
    "tertiaryColor": "#E8F4F8",
    "tertiaryTextColor": "#21295C",
    "lineColor": "#21295C",
    "textColor": "#21295C",
    "mainBkg": "#E8F4F8",
    "nodeBorder": "#065A82",
    "clusterBkg": "#F0F7FA",
    "titleColor": "#21295C",
    "edgeLabelBackground": "#FFFFFF"
  }
}
```

```bash
cat > diagram.mmd << 'EOF'
graph LR
    A[Client] --> B[API Gateway]
    B --> C[Service A]
    B --> D[Service B]
EOF

# Prefer high resolution export for PPT usage
mmdc -i diagram.mmd -o diagram.png -c mermaid-config.json -b white -w 1600 --scale 2
```

### Image Naming Convention

**For ImageGen outputs, include the aspect ratio suffix in the filename:**

```
{descriptive-name}_{ratio}.png
```

The ratio suffix corresponds to the `image_size` enum used:

| `image_size` enum | Filename suffix |
|-------------------|-----------------|
| `landscape_16_9` | `_16x9` |
| `landscape_4_3` | `_4x3` |
| `square` | `_1x1` |
| `portrait_4_3` | `_3x4` |
| `portrait_16_9` | `_9x16` |

Examples:
- `bg-cover_16x9.png` - 16:9 background image
- `image-solution_4x3.png` - 4:3 content illustration
- `image-team_1x1.png` - square headshot/icon
- `bg-section_16x9.png` - 16:9 section background

**When writing the PPT script, extract the ratio from the filename to set correct target `w` and `h`:**

```javascript
// Ratio mapping for target dimensions
const RATIO_MAP = {
  '16x9': { ratio: 16/9 },
  '4x3':  { ratio: 4/3 },
  '1x1':  { ratio: 1 },
  '3x4':  { ratio: 3/4 },
  '9x16': { ratio: 9/16 },
};

// Example: place a 4:3 image in a 4" wide area
// w=4.0, h=4.0/(4/3)=3.0 → correct 4:3 target
```

**For matplotlib/mermaid outputs, use plain descriptive names (no ratio suffix needed):**

```python
# Matplotlib example
fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
# ... create chart ...
plt.savefig('chart-revenue.png', dpi=150, transparent=True)
```

---

## Images

### Image Sources

```javascript
// From file path
slide.addImage({ path: "images/chart.png", x: 1, y: 1, w: 5, h: 3 });

// From URL
slide.addImage({ path: "https://example.com/image.jpg", x: 1, y: 1, w: 5, h: 3 });

// From base64 (faster, no file I/O)
slide.addImage({ data: "image/png;base64,iVBORw0KGgo...", x: 1, y: 1, w: 5, h: 3 });
```

### Image Options

```javascript
slide.addImage({
  path: "image.png",
  x: 1, y: 1, w: 5, h: 3,
  rotate: 45,              // 0-359 degrees
  rounding: true,          // Circular crop
  transparency: 50,        // 0-100
  flipH: true,             // Horizontal flip
  flipV: false,            // Vertical flip
  altText: "Description",  // Accessibility
  hyperlink: { url: "https://example.com" }
});
```

### Image Sizing Modes

```javascript
// Contain - fit inside, preserve ratio
{ sizing: { type: 'contain', w: 4, h: 3 } }

// Cover - fill area, preserve ratio (may crop)
{ sizing: { type: 'cover', w: 4, h: 3 } }

// Crop - cut specific portion
{ sizing: { type: 'crop', x: 0.5, y: 0.5, w: 2, h: 2 } }
```

### Calculate Dimensions (preserve aspect ratio)

```javascript
const origWidth = 1978, origHeight = 923, maxHeight = 3.0;
const calcWidth = maxHeight * (origWidth / origHeight);
const centerX = (10 - calcWidth) / 2;

slide.addImage({ path: "image.png", x: centerX, y: 1.2, w: calcWidth, h: maxHeight });
```

### Supported Formats

- **Standard**: PNG, JPG, GIF (animated GIFs work in Microsoft 365)
- **SVG**: Works in modern PowerPoint/Microsoft 365

---

## Icons

Use react-icons to generate SVG icons, then rasterize to PNG for universal compatibility.

### Setup

```javascript
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const { FaCheckCircle, FaChartLine } = require("react-icons/fa");

function renderIconSvg(IconComponent, color = "#000000", size = 256) {
  return ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(size) })
  );
}

async function iconToBase64Png(IconComponent, color, size = 256) {
  const svg = renderIconSvg(IconComponent, color, size);
  const pngBuffer = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + pngBuffer.toString("base64");
}
```

### Add Icon to Slide

```javascript
const iconData = await iconToBase64Png(FaCheckCircle, "#4472C4", 256);

slide.addImage({
  data: iconData,
  x: 1, y: 1, w: 0.5, h: 0.5  // Size in inches
});
```

**Note**: Use size 256 or higher for crisp icons. The size parameter controls the rasterization resolution, not the display size on the slide (which is set by `w` and `h` in inches).

### Icon Libraries

Install: `npm install -g react-icons react react-dom sharp`

Popular icon sets in react-icons:
- `react-icons/fa` - Font Awesome
- `react-icons/md` - Material Design
- `react-icons/hi` - Heroicons
- `react-icons/bi` - Bootstrap Icons

---

## Slide Backgrounds

```javascript
// Solid color
slide.background = { color: "F1F1F1" };

// Color with transparency
slide.background = { color: "FF3399", transparency: 50 };

// Image from URL
slide.background = { path: "https://example.com/bg.jpg" };

// Image from base64
slide.background = { data: "image/png;base64,iVBORw0KGgo..." };
```

---

## Tables

```javascript
slide.addTable([
  ["Header 1", "Header 2"],
  ["Cell 1", "Cell 2"]
], {
  x: 1, y: 1, w: 8, h: 2,
  border: { pt: 1, color: "999999" }, fill: { color: "F1F1F1" }
});

// Advanced with merged cells
let tableData = [
  [{ text: "Header", options: { fill: { color: "6699CC" }, color: "FFFFFF", bold: true } }, "Cell"],
  [{ text: "Merged", options: { colspan: 2 } }]
];
slide.addTable(tableData, { x: 1, y: 3.5, w: 8, colW: [4, 4] });
```

---

## Charts

```javascript
// Bar chart
slide.addChart(pres.charts.BAR, [{
  name: "Sales", labels: ["Q1", "Q2", "Q3", "Q4"], values: [4500, 5500, 6200, 7100]
}], {
  x: 0.5, y: 0.6, w: 6, h: 3, barDir: 'col',
  showTitle: true, title: 'Quarterly Sales'
});

// Line chart
slide.addChart(pres.charts.LINE, [{
  name: "Temp", labels: ["Jan", "Feb", "Mar"], values: [32, 35, 42]
}], { x: 0.5, y: 4, w: 6, h: 3, lineSize: 3, lineSmooth: true });

// Pie chart
slide.addChart(pres.charts.PIE, [{
  name: "Share", labels: ["A", "B", "Other"], values: [35, 45, 20]
}], { x: 7, y: 1, w: 5, h: 4, showPercent: true });
```

### Better-Looking Charts

Default charts look dated. Apply these options for a modern, clean appearance:

```javascript
slide.addChart(pres.charts.BAR, chartData, {
  x: 0.5, y: 1, w: 9, h: 4, barDir: "col",

  // Custom colors (match your presentation palette)
  chartColors: ["0D9488", "14B8A6", "5EEAD4"],

  // Clean background
  chartArea: { fill: { color: "FFFFFF" }, roundedCorners: true },

  // Muted axis labels
  catAxisLabelColor: "64748B",
  valAxisLabelColor: "64748B",

  // Subtle grid (value axis only)
  valGridLine: { color: "E2E8F0", size: 0.5 },
  catGridLine: { style: "none" },

  // Data labels on bars
  showValue: true,
  dataLabelPosition: "outEnd",
  dataLabelColor: "1E293B",

  // Hide legend for single series
  showLegend: false,
});
```

**Key styling options:**
- `chartColors: [...]` - hex colors for series/segments
- `chartArea: { fill, border, roundedCorners }` - chart background
- `catGridLine/valGridLine: { color, style, size }` - grid lines (`style: "none"` to hide)
- `lineSmooth: true` - curved lines (line charts)
- `legendPos: "r"` - legend position: "b", "t", "l", "r", "tr"

---

## Slide Masters

```javascript
pres.defineSlideMaster({
  title: 'TITLE_SLIDE', background: { color: '283A5E' },
  objects: [{
    placeholder: { options: { name: 'title', type: 'title', x: 1, y: 2, w: 8, h: 2 } }
  }]
});

let titleSlide = pres.addSlide({ masterName: "TITLE_SLIDE" });
titleSlide.addText("My Title", { placeholder: "title" });
```

---

## Common Pitfalls

⚠️ These issues cause file corruption, visual bugs, or broken output. Avoid them.

1. **ALWAYS declare slide dimension constants FIRST** - define `SLIDE_W`, `SLIDE_H`, `CONTENT_W`, `CONTENT_H`, etc. before any content (see Setup & Basic Structure section). Use these constants for ALL positioning to prevent overflow.

2. **ALWAYS include the container system code** - add it right after dimension constants (see Setup & Basic Structure section). This provides text overflow protection and nested containers.

3. **ALWAYS call `slide.render()` at the end of each slide** - without this, nothing will appear in the output file.

4. **NEVER use "#" with hex colors** - causes file corruption
   ```javascript
   color: "FF0000"      // ✅ CORRECT
   color: "#FF0000"     // ❌ WRONG
   ```

5. **Use `bullet: true`** - NEVER unicode symbols like "•" (creates double bullets)

6. **Use `breakLine: true`** between array items or text runs together

7. **Avoid `lineSpacing` with bullets** - causes excessive gaps; use `paraSpaceAfter` instead

8. **Each presentation needs fresh instance** - don't reuse `pptxgen()` objects

9. **Don't use `ROUNDED_RECTANGLE` with accent borders** - rectangular overlay bars won't cover rounded corners. Use `RECTANGLE` instead.
   ```javascript
   // ❌ WRONG: Accent bar doesn't cover rounded corners
   slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 1, y: 1, w: 3, h: 1.5, fill: { color: "FFFFFF" } });
   slide.addShape(pres.shapes.RECTANGLE, { x: 1, y: 1, w: 0.08, h: 1.5, fill: { color: "0891B2" } });

   // ✅ CORRECT: Use RECTANGLE for clean alignment
   slide.addShape(pres.shapes.RECTANGLE, { x: 1, y: 1, w: 3, h: 1.5, fill: { color: "FFFFFF" } });
   slide.addShape(pres.shapes.RECTANGLE, { x: 1, y: 1, w: 0.08, h: 1.5, fill: { color: "0891B2" } });
   ```

10. **NEVER encode opacity in hex color strings** — 8-char colors (e.g., `"00000020"`) corrupt the file. Use the `opacity` property instead.
   ```javascript
   shadow: { type: "outer", blur: 12, offset: 4, color: "00000020" }          // ❌ CORRUPTS FILE
   shadow: { type: "outer", blur: 12, offset: 4, color: "000000", opacity: 0.28 }  // ✅ CORRECT
   ```

11. **NEVER reuse option objects across calls** — PptxGenJS mutates objects in-place (e.g. converting shadow values to EMU). Sharing one object between multiple calls corrupts the second shape.
   ```javascript
   const shadow = { type: "outer", blur: 12, offset: 4, color: "000000", opacity: 0.28 };
   slide.addShape(pres.shapes.RECTANGLE, { shadow, ... });  // ❌ second call gets already-converted values
   slide.addShape(pres.shapes.RECTANGLE, { shadow, ... });

   const makeShadow = () => ({ type: "outer", blur: 12, offset: 4, color: "000000", opacity: 0.28 });
   slide.addShape(pres.shapes.RECTANGLE, { shadow: makeShadow(), ... });  // ✅ fresh object each time
   slide.addShape(pres.shapes.RECTANGLE, { shadow: makeShadow(), ... });
   ```

---

## Post-Generation Fix & Validation (MANDATORY)

After generating the PPTX file, first run the post-processing fix, then layout validation:

```bash
python3 scripts/fix_pptx.py output.pptx
python3 scripts/validate_layout.py output.pptx
```

Fix only the affected slides and re-generate until 0 issues remain. See [SKILL.md — Verification Loop](SKILL.md#verification-loop) for the full fix-and-verify process.

---

## Quick Reference

- **Shapes**: RECTANGLE, OVAL, LINE, ROUNDED_RECTANGLE
- **Charts**: BAR, LINE, PIE, DOUGHNUT, SCATTER, BUBBLE, RADAR
- **Layouts**: LAYOUT_16x9 (10"×5.625"), LAYOUT_16x10, LAYOUT_4x3, LAYOUT_WIDE (13.3"×7.5")
- **Alignment**: "left", "center", "right"
- **Chart data labels**: "outEnd", "inEnd", "center"
