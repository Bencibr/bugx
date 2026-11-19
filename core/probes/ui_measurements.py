"""Browser-side measurements for the Web UI audit."""

# The script receives validated data as an argument. Selectors and CSS values are
# never interpolated into JavaScript source.
MEASURE_UI = r"""
({tokens, components, contrast, reflow}) => {
  const root = document.documentElement;
  const style = (node) => getComputedStyle(node);
  const limit = 400;
  const rect = (node) => node.getBoundingClientRect();
  const visible = (node) => {
    const {width, height} = rect(node);
    return width > 0 && height > 0 && node.checkVisibility({
      checkOpacity: true, checkVisibilityCSS: true,
    });
  };
  const locator = (node) => {
    const parts = [];
    for (let el = node; el && el !== root; el = el.parentElement) {
      if (el.id) {
        const id = '#' + CSS.escape(el.id);
        if (document.querySelectorAll(id).length === 1) {
          parts.unshift(id);
          break;
        }
      }
      let position = 1;
      for (let sib = el.previousElementSibling; sib; sib = sib.previousElementSibling) {
        if (sib.localName === el.localName) position++;
      }
      parts.unshift(CSS.escape(el.localName) + ':nth-of-type(' + position + ')');
    }
    return parts.join(' > ') || 'html';
  };
  const round = (value) => Number(value.toFixed(3));
  const measured = (id, status, detail) => ({id, status, ...detail});
  const outcome = (rows) => rows.some((row) => row.status === 'FAIL') ? 'FAIL'
    : !rows.length || rows.some((row) => row.status === 'UNVERIFIED') ? 'UNVERIFIED' : 'PASS';

  const canvas = document.createElement('canvas').getContext('2d');
  const rgba = (value) => {
    if (!value || !CSS.supports('color', value) || /currentcolor|var\(/i.test(value)) return null;
    // A wide-gamut canvas color is serialized as color(...). Limit WCAG
    // arithmetic to colors the browser resolves to sRGB, without clamping.
    canvas.fillStyle = '#010203';
    canvas.fillStyle = value;
    const normalized = canvas.fillStyle;
    if (normalized === '#010203') {
      canvas.fillStyle = '#040506';
      canvas.fillStyle = value;
      if (canvas.fillStyle === '#040506') return null;
    }
    if (/^#[0-9a-f]{6}$/i.test(normalized)) {
      return [1, 3, 5].map((start) => parseInt(normalized.slice(start, start + 2), 16)).concat(1);
    }
    const match = /^rgba?\(\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)(?:,\s*(\d*(?:\.\d+)?))?\)$/.exec(normalized);
    return match ? [Number(match[1]), Number(match[2]), Number(match[3]), Number(match[4] ?? 1)] : null;
  };
  const over = (front, back) => {
    const alpha = front[3] + back[3] * (1 - front[3]);
    if (!alpha) return [0, 0, 0, 0];
    return front.slice(0, 3).map((v, i) =>
      (v * front[3] + back[i] * back[3] * (1 - front[3])) / alpha).concat(alpha);
  };
  const scheme = style(root).colorScheme.split(/\s+/);
  const darkCanvas = scheme.includes('dark') &&
    (!scheme.includes('light') || matchMedia('(prefers-color-scheme: dark)').matches);
  const base = [255, 255, 255, 1];
  const simplePaint = (el) => {
    for (let node = el; node; node = node.parentElement) {
      const s = style(node);
      if (s.opacity !== '1' || s.filter !== 'none' || s.backdropFilter !== 'none' ||
          s.mixBlendMode !== 'normal' || s.maskImage !== 'none') return false;
      for (const pseudo of ['::before', '::after']) {
        const p = getComputedStyle(node, pseudo);
        if (p.content !== 'none' && p.content !== 'normal' &&
            (p.backgroundImage !== 'none' || p.backgroundColor !== 'rgba(0, 0, 0, 0)' ||
             p.position === 'absolute' || p.position === 'fixed')) return false;
      }
    }
    return true;
  };
  const surface = (el) => {
    const layers = [];
    for (let node = el; node; node = node.parentElement) {
      const s = style(node);
      if (s.backgroundImage !== 'none' || s.backgroundClip === 'text') return null;
      const paint = rgba(s.backgroundColor);
      if (!paint) return null;
      layers.push(paint);
      if (paint[3] === 1) return layers.reverse().reduce((under, top) => over(top, under), base);
    }
    // Do not infer a dark canvas color from the OS/Chromium theme.
    return darkCanvas ? null : layers.reverse().reduce((under, top) => over(top, under), base);
  };
  const luminance = (rgb) => rgb.slice(0, 3).reduce((sum, value, i) => {
    const v = value / 255;
    const linear = v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    return sum + linear * [0.2126, 0.7152, 0.0722][i];
  }, 0);
  const ratio = (fg, bg) => {
    const light = luminance(over(fg, bg));
    const dark = luminance(bg);
    return (Math.max(light, dark) + 0.05) / (Math.min(light, dark) + 0.05);
  };
  const obscured = (node, box) => {
    const x = (Math.max(box.left, 0) + Math.min(box.right, innerWidth)) / 2;
    const y = (Math.max(box.top, 0) + Math.min(box.bottom, innerHeight)) / 2;
    const hit = document.elementFromPoint(x, y);
    return hit && !node.contains(hit) && !hit.contains(node);
  };
  const onScreen = (box) =>
    box.width > 1 && box.height > 1 && box.right > 0 && box.bottom > 0 &&
    box.left < innerWidth && box.top < innerHeight;
  const inactive = (node) => node.closest(':disabled,[aria-disabled="true"]');
  const textFailures = [];
  const textUnknown = [];
  let samples = 0;
  let unknownCount = 0;
  let minimumRatio = null;
  const unknown = (node, reason) => {
    unknownCount++;
    if (textUnknown.length < 24) textUnknown.push({selector: locator(node), reason});
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let visited = 0;
  let textNode;
  while ((textNode = walker.nextNode())) {
    if (++visited > 12000 || samples + unknownCount >= limit) {
      unknown(document.body, 'sample-limit');
      break;
    }
    const node = textNode.parentElement;
    if (!textNode.data.trim() || !node || node.closest('script,style,noscript,template,option,svg') ||
        !visible(node) || inactive(node)) continue;
    const range = document.createRange();
    range.selectNodeContents(textNode);
    const boxes = Array.from(range.getClientRects()).filter(onScreen);
    if (!boxes.length) continue;
    const s = style(node);
    const fg = rgba(s.webkitTextFillColor || s.color);
    const bg = simplePaint(node) && !boxes.some((box) => obscured(node, box)) ? surface(node) : null;
    if (!fg || !fg[3] || !bg || s.textShadow !== 'none' || s.webkitTextStrokeWidth !== '0px') {
      unknown(node, 'complex-text-paint');
      continue;
    }
    const size = parseFloat(s.fontSize);
    const bold = parseFloat(s.fontWeight) >= 700;
    const threshold = size >= 24 || (size >= 56 / 3 - 0.001 && bold) ? 3 : 4.5;
    const value = ratio(fg, bg);
    samples++;
    minimumRatio = minimumRatio === null ? value : Math.min(value, minimumRatio);
    if (value < threshold) {
      textFailures.push({selector: locator(node), ratio: value, threshold, fg, bg});
    }
  }
  for (const node of document.querySelectorAll('iframe,canvas,svg text,input,textarea,select')) {
    if (!onScreen(rect(node)) || !visible(node) || inactive(node)) continue;
    if (node.matches('iframe,canvas,svg text') || node.value || node.placeholder ||
        node.matches('input[type=submit],input[type=reset]')) {
      unknown(node, 'embedded-or-native-text');
    }
    if (unknownCount >= limit) break;
  }
  const textContrast = {
    status: textFailures.length ? 'FAIL' : !samples || unknownCount ? 'UNVERIFIED' : 'PASS',
    samples, unknown_count: unknownCount, unknown: textUnknown, failures: textFailures,
    min_ratio: minimumRatio === null ? null : round(minimumRatio),
  };

  const rootStyle = style(root);
  const sameColor = (left, right) => {
    const first = rgba(left), second = rgba(right);
    return first && second && first.every((n, i) => Math.abs(n - second[i]) < 0.0001);
  };
  const tokenChecks = Object.entries(tokens).map(([name, expected]) => {
    const actual = rootStyle.getPropertyValue(name).trim();
    const matches = actual && (expected === null || actual === expected || sameColor(actual, expected));
    return measured(name, matches ? 'PASS' : 'FAIL', {actual, expected});
  });
  const cssLength = (value, property, computed) => {
    const match = /^(-?\d+(?:\.\d+)?)(px|rem|em)?$/.exec(value);
    if (!match) return null;
    const n = Number(match[1]);
    if (match[2] === 'rem') return n * parseFloat(rootStyle.fontSize);
    if (match[2] === 'em' && property !== 'font-size') return n * parseFloat(computed.fontSize);
    return match[2] === 'px' || n === 0 ? n : null;
  };
  const compareCSS = (property, expected, actual, computed) => {
    if (!expected || !CSS.supports(property, expected)) return 'UNVERIFIED';
    if (actual === expected || (property.endsWith('color') && sameColor(actual, expected))) return 'PASS';
    if (property === 'font-family') {
      const families = (value) => value.replace(/['"]/g, '').toLowerCase().split(',').map(s => s.trim());
      return String(families(actual)) === String(families(expected)) ? 'PASS' : 'FAIL';
    }
    if (property === 'font-weight') {
      const weight = (value) => ({normal: '400', bold: '700'})[value] || value;
      return weight(actual) === weight(expected) ? 'PASS' : 'FAIL';
    }
    const left = cssLength(actual, property, computed), right = cssLength(expected, property, computed);
    if (left !== null && right !== null) return Math.abs(left - right) <= 0.02 ? 'PASS' : 'FAIL';
    if (/var\(|calc\(|%|(?:^|\d)em\b|currentcolor/i.test(expected)) return 'UNVERIFIED';
    return 'FAIL';
  };
  const componentChecks = components.map((rule) => {
    const matches = Array.from(document.querySelectorAll(rule.selector)).filter(visible);
    const checks = [];
    for (const node of matches.slice(0, 48)) {
      const s = style(node);
      for (const [prop, ref] of Object.entries(rule.styles || {})) {
        const expected = ref.startsWith('--') ? s.getPropertyValue(ref).trim() : ref;
        const actual = s.getPropertyValue(prop).trim();
        const status = compareCSS(prop, expected, actual, s);
        checks.push(measured(prop, status, {selector: locator(node), actual, expected, reference: ref}));
      }
    }
    if (rule.align) {
      const axis = (box) => rule.align === 'center-x' ? box.left + box.width / 2 : box[rule.align];
      const coordinates = matches.slice(0, 48).map((node) => axis(rect(node)));
      const spread = coordinates.length >= 2 ? Math.max(...coordinates) - Math.min(...coordinates) : null;
      const tolerance = rule.tolerance_px ?? 2;
      const status = spread === null ? 'UNVERIFIED' : spread <= tolerance ? 'PASS' : 'FAIL';
      checks.push(measured('alignment', status, {axis: rule.align, spread_px: spread, tolerance_px: tolerance}));
    }
    if (matches.length > 48) checks.push(measured('coverage', 'UNVERIFIED', {reason: 'component sample limit'}));
    const status = matches.length ? outcome(checks) : 'FAIL';
    return measured(rule.id, status, {selector: rule.selector, matches: matches.length, checks});
  });

  const active = document.activeElement;
  const keyboardFocus = !!active && active !== document.body && active !== root &&
    visible(active) && active.matches(':focus-visible') && onScreen(rect(active)) &&
    !obscured(active, rect(active));
  const nonText = contrast.map((rule) => {
    const matches = Array.from(document.querySelectorAll(rule.selector)).filter(visible);
    const checks = [];
    for (const node of matches.slice(0, 16)) {
      const selector = locator(node);
      if (rule.focus) node.focus({preventScroll: true});
      if (rule.focus && (document.activeElement !== node || !node.matches(':focus-visible'))) {
        checks.push(measured('focus', 'UNVERIFIED', {selector, reason: 'focus state unavailable'}));
        continue;
      }
      const s = style(node);
      const kind = rule.property;
      if ((kind === 'outline-color' && (!parseFloat(s.outlineWidth) || s.outlineStyle === 'none')) ||
          (kind === 'border-top-color' && (!parseFloat(s.borderTopWidth) || s.borderTopStyle === 'none'))) {
        checks.push(measured(kind, 'FAIL', {selector, reason: 'declared indicator absent'}));
        continue;
      }
      const fg = kind === 'background-color' && s.backgroundImage !== 'none' ?
        null : rgba(s.getPropertyValue(kind));
      const bg = simplePaint(node) && surface(rule.against === 'self' ? node : node.parentElement);
      const value = fg && bg ? ratio(fg, bg) : null;
      checks.push(measured(kind, value === null ? 'UNVERIFIED' : value < 3 ? 'FAIL' : 'PASS',
        {selector, ratio: value, threshold: 3, fg, bg}));
    }
    if (matches.length > 16) checks.push(measured('coverage', 'UNVERIFIED', {reason: 'indicator sample limit'}));
    return measured(rule.id, outcome(checks), {selector: rule.selector, matches: matches.length, checks});
  });
  if (active instanceof HTMLElement && active !== document.activeElement) {
    if (active === document.body) document.activeElement?.blur();
    else active.focus({preventScroll: true});
  }

  const viewport = {
    width: innerWidth, height: innerHeight, dpr: devicePixelRatio,
    client_width: root.clientWidth, scroll_width: Math.max(root.scrollWidth, document.body.scrollWidth),
  };
  const horizontalOverflow = Math.max(0, viewport.scroll_width - viewport.client_width);
  const anchors = reflow.map((rule) => {
    const matches = document.querySelectorAll(rule.selector);
    const node = matches.length === 1 ? matches[0] : null;
    const shown = !!node && visible(node);
    const box = shown ? rect(node) : null;
    const outside = !!box && (box.left + scrollX < -1 || box.right + scrollX > root.clientWidth + 1);
    let clipped = false;
    for (let parent = node; box && parent && parent !== document.body; parent = parent.parentElement) {
      const s = style(parent), bounds = rect(parent);
      const clippedAxis = (overflow) => overflow === 'hidden' || overflow === 'clip';
      clipped ||= clippedAxis(s.overflowX) &&
        (box.left < bounds.left - 1 || box.right > bounds.right + 1 ||
         (parent === node && parent.scrollWidth > parent.clientWidth + 1));
      clipped ||= clippedAxis(s.overflowY) &&
        (box.top < bounds.top - 1 || box.bottom > bounds.bottom + 1 ||
         (parent === node && parent.scrollHeight > parent.clientHeight + 1));
    }
    return measured(rule.id, shown && !outside && !clipped ? 'PASS' : 'FAIL', {
      selector: rule.selector, matches: matches.length, shown, outside, clipped,
      rect: box ? {x: round(box.x), y: round(box.y), width: round(box.width), height: round(box.height)} : null,
    });
  });
  return {viewport, horizontal_overflow_px: horizontalOverflow, anchors, keyboard_focus: keyboardFocus,
    text_contrast: textContrast, non_text_contrast: nonText, tokens: tokenChecks, components: componentChecks};
}
"""

CHECK_JOURNEY = r"""
(checks) => {
  return checks.map((rule) => {
    const matches = document.querySelectorAll(rule.selector);
    const node = matches.length === 1 ? matches[0] : null;
    const box = node?.getBoundingClientRect();
    const visible = !!box && box.width > 0 && box.height > 0 &&
      node.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
    const status = (rule.visible === false ? !visible && matches.length <= 1 : visible) &&
      (rule.text === undefined || (node.innerText || '').includes(rule.text)) &&
      (rule.disabled === undefined ||
        !!node.closest(':disabled,[aria-disabled="true"]') === rule.disabled) &&
      Object.entries(rule.attributes || {}).every(([name, value]) => node.getAttribute(name) === value);
    return {selector: rule.selector, matches: matches.length, visible, passed: status};
  });
}
"""
