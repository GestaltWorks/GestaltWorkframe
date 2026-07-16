/**
 * Chaos entry engine: renders a brand logo as a particle field that bursts
 * into a randomly chosen fractal attractor, holds, then converges into a
 * rectangular frame around a target element (the terminal panel).
 *
 * Brand-agnostic by design: the logo arrives as an image element, the palette
 * as options. No React, no framework dependencies — a canvas and rAF only.
 *
 * Fractal sources: affine IFS tables played via the "chaos game", plus two
 * strange attractors (Clifford / De Jong) iterated directly.
 */

export type EntryPhase = "burst" | "fractal" | "framing" | "framed";

export type ChaosColors = {
  dark: string;
  gold: string;
  goldWarm: string;
  sage: string;
  offwhite: string;
};

export type ChaosEngineOptions = {
  canvas: HTMLCanvasElement;
  colors?: Partial<ChaosColors>;
  particleCount?: number;
  fractalHoldMs?: number;
  onPhase?: (phase: EntryPhase, attractorName: string) => void;
};

type AffineAttractor = {
  name: string;
  /** [a, b, c, d, e, f, p] per map: x' = ax + by + e ; y' = cx + dy + f */
  maps: number[][];
};

type StrangeAttractor = {
  name: string;
  kind: "clifford" | "dejong";
  params: [number, number, number, number];
};

type Attractor = AffineAttractor | StrangeAttractor;

const DEFAULT_COLORS: ChaosColors = {
  dark: "#242128",
  gold: "#DCD077",
  goldWarm: "#D4BF91",
  sage: "#566B5B",
  offwhite: "#F5F5F5",
};

const ATTRACTORS: Attractor[] = [
  { name: "Barnsley Fern", maps: [
    [0.0, 0.0, 0.0, 0.16, 0.0, 0.0, 0.01],
    [0.85, 0.04, -0.04, 0.85, 0.0, 1.6, 0.85],
    [0.2, -0.26, 0.23, 0.22, 0.0, 1.6, 0.07],
    [-0.15, 0.28, 0.26, 0.24, 0.0, 0.44, 0.07],
  ] },
  { name: "Sierpinski Gasket", maps: [
    [0.5, 0, 0, 0.5, 0.0, 0.0, 0.34],
    [0.5, 0, 0, 0.5, 0.5, 0.0, 0.33],
    [0.5, 0, 0, 0.5, 0.25, 0.5, 0.33],
  ] },
  { name: "Spiral Nebula", maps: [
    [0.787879, -0.424242, 0.242424, 0.859848, 1.758647, 1.408065, 0.9],
    [-0.121212, 0.257576, 0.151515, 0.05303, -6.721654, 1.377236, 0.05],
    [0.181818, -0.136364, 0.090909, 0.181818, 6.086107, 1.568035, 0.05],
  ] },
  { name: "Maple Leaf", maps: [
    [0.14, 0.01, 0.0, 0.51, -0.08, -1.31, 0.1],
    [0.43, 0.52, -0.45, 0.5, 1.49, -0.75, 0.35],
    [0.45, -0.49, 0.47, 0.47, -1.62, -0.74, 0.35],
    [0.49, 0.0, 0.0, 0.51, 0.02, 1.62, 0.2],
  ] },
  { name: "Heighway Dragon", maps: [
    [0.5, -0.5, 0.5, 0.5, 0, 0, 0.5],
    [-0.5, -0.5, 0.5, -0.5, 1, 0, 0.5],
  ] },
  { name: "Symmetric Tree", maps: [
    [0.0, 0.0, 0.0, 0.5, 0, 0.0, 0.05],
    [0.42, -0.42, 0.42, 0.42, 0, 0.2, 0.4],
    [0.42, 0.42, -0.42, 0.42, 0, 0.2, 0.4],
    [0.1, 0.0, 0.0, 0.1, 0, 0.2, 0.15],
  ] },
  { name: "Fishbone Fern", maps: [
    [0.0, 0.0, 0.0, 0.25, 0.0, -0.4, 0.02],
    [0.95, 0.005, -0.005, 0.93, -0.002, 0.5, 0.84],
    [0.035, -0.2, 0.16, 0.04, -0.09, 0.02, 0.07],
    [-0.04, 0.2, 0.16, 0.04, 0.083, 0.12, 0.07],
  ] },
  { name: "Lévy C Curve", maps: [
    [0.5, 0.5, -0.5, 0.5, 0.0, 0.0, 0.5],
    [0.5, -0.5, 0.5, 0.5, 0.5, -0.5, 0.5],
  ] },
  { name: "Crystal", maps: [
    [0.69697, -0.481061, -0.393939, -0.662879, 2.147003, 10.310288, 0.75],
    [0.090909, -0.443182, 0.515152, -0.094697, 4.286558, 2.925762, 0.25],
  ] },
  { name: "Pentadendrite", maps: [
    [0.341, -0.071, 0.071, 0.341, 0.0, 0.0, 0.167],
    [0.038, -0.346, 0.346, 0.038, 0.341, 0.071, 0.167],
    [0.341, -0.071, 0.071, 0.341, 0.379, 0.418, 0.167],
    [-0.234, 0.258, -0.258, -0.234, 0.72, 0.489, 0.166],
    [0.173, 0.302, -0.302, 0.173, 0.486, 0.231, 0.166],
    [0.341, -0.071, 0.071, 0.341, 0.679, -0.069, 0.167],
  ] },
  { name: "Sierpinski Carpet", maps: [
    [1 / 3, 0, 0, 1 / 3, 0, 0, 0.125], [1 / 3, 0, 0, 1 / 3, 1 / 3, 0, 0.125],
    [1 / 3, 0, 0, 1 / 3, 2 / 3, 0, 0.125], [1 / 3, 0, 0, 1 / 3, 0, 1 / 3, 0.125],
    [1 / 3, 0, 0, 1 / 3, 2 / 3, 1 / 3, 0.125], [1 / 3, 0, 0, 1 / 3, 0, 2 / 3, 0.125],
    [1 / 3, 0, 0, 1 / 3, 1 / 3, 2 / 3, 0.125], [1 / 3, 0, 0, 1 / 3, 2 / 3, 2 / 3, 0.125],
  ] },
  { name: "Strange Bloom", kind: "clifford", params: [-1.4, 1.6, 1.0, 0.7] },
  { name: "Ghost Orchid", kind: "dejong", params: [1.4, -2.3, 2.4, -2.1] },
];

const BURST_MS = 620;
const CONVERGE_MS = 2400;
const MAX_COLOR_BUCKETS = 255;

/** Session-scoped memory so consecutive visits avoid an immediate repeat. */
const LAST_ATTRACTOR_KEY = "chaos-entry-last-attractor";

function pickAttractorIndex(): number {
  let last = -1;
  try {
    last = Number(sessionStorage.getItem(LAST_ATTRACTOR_KEY) ?? -1);
  } catch {
    /* storage unavailable: repeats are acceptable */
  }
  let idx = Math.floor(Math.random() * ATTRACTORS.length);
  if (ATTRACTORS.length > 1 && idx === last) {
    idx = (idx + 1 + Math.floor(Math.random() * (ATTRACTORS.length - 1))) % ATTRACTORS.length;
  }
  try {
    sessionStorage.setItem(LAST_ATTRACTOR_KEY, String(idx));
  } catch {
    /* ignore */
  }
  return idx;
}

function smoothstep(t: number): number {
  return t * t * (3 - 2 * t);
}

function stepStrange(att: StrangeAttractor, x: number, y: number): [number, number] {
  const [a, b, c, d] = att.params;
  if (att.kind === "clifford") {
    return [Math.sin(a * y) + c * Math.cos(a * x), Math.sin(b * x) + d * Math.cos(b * y)];
  }
  return [Math.sin(a * y) - Math.cos(b * x), Math.sin(c * x) - Math.cos(d * y)];
}

/** Color a strange-attractor point by the direction it just jumped. */
function flowGroup(dx: number, dy: number): number {
  return Math.floor(((Math.atan2(dy, dx) + Math.PI) / (2 * Math.PI)) * 4) & 3;
}

function withAlpha(hex: string, alpha: number): string {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export class ChaosEngine {
  private readonly canvas: HTMLCanvasElement;
  private readonly ctx: CanvasRenderingContext2D;
  private readonly colors: ChaosColors;
  private readonly n: number;
  private readonly fractalHoldMs: number;
  private readonly onPhase?: (phase: EntryPhase, attractorName: string) => void;

  private width = 0;
  private height = 0;
  private dpr = 1;

  // particle state
  private readonly px: Float32Array;
  private readonly py: Float32Array;
  private readonly vx: Float32Array;
  private readonly vy: Float32Array;
  private readonly tx: Float32Array;
  private readonly ty: Float32Array;
  private readonly fx: Float64Array;
  private readonly fy: Float64Array;
  private readonly colorGroup: Uint8Array;   // fractal color group (0..3)
  private readonly logoBucket: Uint8Array;   // quantized logo color per particle
  private readonly seed: Float32Array;

  private logoBucketStyles: string[] = [];
  private logoOrder: Uint32Array;            // particle indices grouped by logo bucket
  private readonly groupStyles: string[];

  /** Internal machine: idle -> burst -> converge -> (fractal | framed). */
  private phase: "idle" | "burst" | "converge" | "fractal" | "framed" = "idle";
  private heading: "fractal" | "frame" = "fractal";
  private phaseStart = 0;
  private attractor: Attractor = ATTRACTORS[0];

  // fractal-space -> screen mapping
  private fScale = 1;
  private fOffsetX = 0;
  private fOffsetY = 0;
  private bounds = { x0: 0, x1: 1, y0: 0, y1: 1 };

  private getFrameRect: (() => DOMRect) | null = null;
  private rafId = 0;
  private destroyed = false;
  private readonly handleResize = () => this.refit();

  constructor(options: ChaosEngineOptions) {
    this.canvas = options.canvas;
    const ctx = options.canvas.getContext("2d", { alpha: false });
    if (!ctx) throw new Error("ChaosEngine: 2d canvas context unavailable");
    this.ctx = ctx;
    this.colors = { ...DEFAULT_COLORS, ...options.colors };
    this.n = options.particleCount ?? 9000;
    this.fractalHoldMs = options.fractalHoldMs ?? 6500;
    this.onPhase = options.onPhase;

    this.px = new Float32Array(this.n);
    this.py = new Float32Array(this.n);
    this.vx = new Float32Array(this.n);
    this.vy = new Float32Array(this.n);
    this.tx = new Float32Array(this.n);
    this.ty = new Float32Array(this.n);
    this.fx = new Float64Array(this.n);
    this.fy = new Float64Array(this.n);
    this.colorGroup = new Uint8Array(this.n);
    this.logoBucket = new Uint8Array(this.n);
    this.seed = new Float32Array(this.n);
    this.logoOrder = new Uint32Array(this.n);

    this.groupStyles = [
      withAlpha(this.colors.gold, 0.55),
      withAlpha(this.colors.goldWarm, 0.55),
      withAlpha(this.colors.sage, 0.75),
      withAlpha(this.colors.offwhite, 0.5),
    ];

    this.resizeCanvas();
    window.addEventListener("resize", this.handleResize);
  }

  get attractorName(): string {
    return this.attractor.name;
  }

  /**
   * Start the sequence: sample the logo image, spawn particles in place over
   * `logoRect` (CSS pixels), burst toward a random attractor, then converge
   * into a frame around `getFrameRect`.
   *
   * Throws if the image pixels are unreadable (e.g. cross-origin taint);
   * callers should catch and fall back to a non-animated reveal.
   */
  ignite(logo: HTMLImageElement, logoRect: DOMRect, getFrameRect: () => DOMRect): void {
    this.getFrameRect = getFrameRect;
    this.sampleLogo(logo, logoRect);

    const index = pickAttractorIndex();
    this.attractor = ATTRACTORS[index];
    this.generateFractalTargets();
    this.heading = "fractal";
    this.beginBurst(
      (logoRect.left + logoRect.width / 2) * this.dpr,
      (logoRect.top + logoRect.height / 2) * this.dpr,
    );

    if (!this.rafId) this.rafId = requestAnimationFrame(this.frame);
  }

  /** Recompute canvas size and current targets (e.g. after window resize). */
  refit(): void {
    this.resizeCanvas();
    if (this.phase === "idle") return;
    if (this.heading === "fractal") this.fitFractalToScreen();
    else this.setFrameTargets();
    if (this.phase === "fractal" || this.phase === "framed") {
      for (let i = 0; i < this.n; i++) {
        this.px[i] = this.tx[i];
        this.py[i] = this.ty[i];
      }
    }
  }

  destroy(): void {
    this.destroyed = true;
    if (this.rafId) cancelAnimationFrame(this.rafId);
    window.removeEventListener("resize", this.handleResize);
  }

  private resizeCanvas(): void {
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.width = this.canvas.width = Math.floor(this.canvas.clientWidth * this.dpr);
    this.height = this.canvas.height = Math.floor(this.canvas.clientHeight * this.dpr);
    this.hardClear();
  }

  /**
   * Repeated low-alpha fades plateau on an 8-bit canvas and leave a ghost of
   * bright pixels; a hard repaint at phase boundaries erases it while the
   * burst masks the cut.
   */
  private hardClear(): void {
    this.ctx.globalCompositeOperation = "source-over";
    this.ctx.fillStyle = this.colors.dark;
    this.ctx.fillRect(0, 0, this.width, this.height);
  }

  private sampleLogo(logo: HTMLImageElement, logoRect: DOMRect): void {
    const iw = logo.naturalWidth;
    const ih = logo.naturalHeight;
    const off = document.createElement("canvas");
    off.width = iw;
    off.height = ih;
    const octx = off.getContext("2d", { willReadFrequently: true });
    if (!octx) throw new Error("ChaosEngine: offscreen context unavailable");
    octx.drawImage(logo, 0, 0);
    const data = octx.getImageData(0, 0, iw, ih).data; // throws when tainted

    const visible: number[] = [];
    for (let y = 0; y < ih; y++) {
      for (let x = 0; x < iw; x++) {
        const o = (y * iw + x) * 4;
        if (data[o + 3] > 60) visible.push(o);
      }
    }
    if (visible.length === 0) throw new Error("ChaosEngine: logo image has no visible pixels");

    // spawn each particle on a random visible logo pixel, in screen space
    const bucketOf = new Map<number, number>();
    const bucketSum: number[][] = [];
    const originX = logoRect.left * this.dpr;
    const originY = logoRect.top * this.dpr;
    const scaleX = (logoRect.width * this.dpr) / iw;
    const scaleY = (logoRect.height * this.dpr) / ih;

    for (let i = 0; i < this.n; i++) {
      const o = visible[Math.floor(Math.random() * visible.length)];
      const pixelIndex = o / 4;
      const x = pixelIndex % iw;
      const y = Math.floor(pixelIndex / iw);
      this.px[i] = originX + (x + Math.random() * 0.6) * scaleX;
      this.py[i] = originY + (y + Math.random() * 0.6) * scaleY;
      this.seed[i] = Math.random();

      const r = data[o], g = data[o + 1], b = data[o + 2];
      const key = ((r >> 5) << 6) | ((g >> 5) << 3) | (b >> 5);
      let bucket = bucketOf.get(key);
      if (bucket === undefined && bucketSum.length < MAX_COLOR_BUCKETS) {
        bucket = bucketSum.length;
        bucketOf.set(key, bucket);
        bucketSum.push([0, 0, 0, 0]);
      }
      if (bucket === undefined) bucket = 0;
      this.logoBucket[i] = bucket;
      const acc = bucketSum[bucket];
      acc[0] += r;
      acc[1] += g;
      acc[2] += b;
      acc[3]++;
    }

    this.logoBucketStyles = bucketSum.map(
      (a) => `rgba(${Math.floor(a[0] / a[3])}, ${Math.floor(a[1] / a[3])}, ${Math.floor(a[2] / a[3])}, 0.95)`,
    );
    // group particle indices by bucket so drawing is one O(n) pass
    const ids = Array.from({ length: this.n }, (_, i) => i);
    ids.sort((a, b2) => this.logoBucket[a] - this.logoBucket[b2]);
    this.logoOrder = Uint32Array.from(ids);
  }

  private generateFractalTargets(): void {
    let x = 0.1;
    let y = 0.1;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;

    if ("maps" in this.attractor) {
      const maps = this.attractor.maps;
      const count = maps.length;
      const cumulative: number[] = [];
      let total = 0;
      for (let m = 0; m < count; m++) {
        total += maps[m][6];
        cumulative.push(total);
      }
      const pick = (): number => {
        const r = Math.random() * total;
        for (let m = 0; m < count; m++) if (r <= cumulative[m]) return m;
        return count - 1;
      };
      for (let i = 0; i < 60; i++) {
        const t = maps[pick()];
        const nx = t[0] * x + t[1] * y + t[4];
        const ny = t[2] * x + t[3] * y + t[5];
        x = nx;
        y = ny;
      }
      for (let i = 0; i < this.n; i++) {
        const m = pick();
        const t = maps[m];
        const nx = t[0] * x + t[1] * y + t[4];
        const ny = t[2] * x + t[3] * y + t[5];
        x = nx;
        y = ny;
        this.fx[i] = x;
        this.fy[i] = y;
        this.colorGroup[i] = m & 3;
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    } else {
      const att = this.attractor;
      for (let i = 0; i < 100; i++) [x, y] = stepStrange(att, x, y);
      for (let i = 0; i < this.n; i++) {
        const [nx, ny] = stepStrange(att, x, y);
        this.colorGroup[i] = flowGroup(nx - x, ny - y);
        x = nx;
        y = ny;
        this.fx[i] = x;
        this.fy[i] = y;
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
    this.bounds = { x0, x1, y0, y1 };
    this.fitFractalToScreen();
  }

  private fitFractalToScreen(): void {
    const vw = Math.max(1e-6, this.bounds.x1 - this.bounds.x0);
    const vh = Math.max(1e-6, this.bounds.y1 - this.bounds.y0);
    this.fScale = Math.min(this.width / vw, this.height / vh) * 0.82;
    this.fOffsetX = (this.width - vw * this.fScale) / 2 - this.bounds.x0 * this.fScale;
    this.fOffsetY = (this.height + vh * this.fScale) / 2 + this.bounds.y0 * this.fScale;
    for (let i = 0; i < this.n; i++) {
      this.tx[i] = this.fx[i] * this.fScale + this.fOffsetX;
      this.ty[i] = this.fOffsetY - this.fy[i] * this.fScale;
    }
  }

  private setFrameTargets(): void {
    if (!this.getFrameRect) return;
    const rect = this.getFrameRect();
    const x0 = rect.left * this.dpr;
    const y0 = rect.top * this.dpr;
    const w = rect.width * this.dpr;
    const h = rect.height * this.dpr;
    const perimeter = 2 * (w + h);
    for (let i = 0; i < this.n; i++) {
      let s = (i / this.n) * perimeter;
      const thickness = (this.seed[i] - 0.5) * 5 * this.dpr;
      if (s < w) {
        this.tx[i] = x0 + s;
        this.ty[i] = y0 + thickness;
      } else if ((s -= w) < h) {
        this.tx[i] = x0 + w + thickness;
        this.ty[i] = y0 + s;
      } else if ((s -= h) < w) {
        this.tx[i] = x0 + w - s;
        this.ty[i] = y0 + h + thickness;
      } else {
        s -= w;
        this.tx[i] = x0 + thickness;
        this.ty[i] = y0 + h - s;
      }
    }
  }

  private beginBurst(centerX: number, centerY: number): void {
    const swirl = Math.random() < 0.5 ? 1 : -1;
    for (let i = 0; i < this.n; i++) {
      const dx = this.px[i] - centerX;
      const dy = this.py[i] - centerY;
      const d = Math.hypot(dx, dy) + 4;
      const dirX = dx / d;
      const dirY = dy / d;
      const speed = (2.5 + Math.random() * 9) * this.dpr * (0.6 + 160 / d);
      this.vx[i] = dirX * speed - dirY * speed * 0.45 * swirl + (Math.random() - 0.5) * 2 * this.dpr;
      this.vy[i] = dirY * speed + dirX * speed * 0.45 * swirl + (Math.random() - 0.5) * 2 * this.dpr;
    }
    this.hardClear();
    this.setPhase("burst");
  }

  private setPhase(phase: "burst" | "converge" | "fractal" | "framed"): void {
    this.phase = phase;
    this.phaseStart = performance.now();
    // public phase names: the converge toward the frame reads as "framing";
    // the converge toward the fractal is still part of the burst, theatrically
    if (phase === "converge") {
      if (this.heading === "frame") this.onPhase?.("framing", this.attractor.name);
      return;
    }
    this.onPhase?.(phase, this.attractor.name);
  }

  private settle(): void {
    this.hardClear();
    for (let i = 0; i < this.n; i++) {
      this.px[i] = this.tx[i];
      this.py[i] = this.ty[i];
      this.vx[i] = 0;
      this.vy[i] = 0;
    }
    this.setPhase(this.heading === "fractal" ? "fractal" : "framed");
  }

  private liveShimmer(): void {
    const jumps = Math.floor(this.n * 0.14);
    if ("maps" in this.attractor) {
      const maps = this.attractor.maps;
      const count = maps.length;
      const cumulative: number[] = [];
      let total = 0;
      for (let m = 0; m < count; m++) {
        total += maps[m][6];
        cumulative.push(total);
      }
      for (let j = 0; j < jumps; j++) {
        const i = Math.floor(Math.random() * this.n);
        const r = Math.random() * total;
        let m = count - 1;
        for (let k = 0; k < count; k++) {
          if (r <= cumulative[k]) {
            m = k;
            break;
          }
        }
        const t = maps[m];
        const nx = t[0] * this.fx[i] + t[1] * this.fy[i] + t[4];
        const ny = t[2] * this.fx[i] + t[3] * this.fy[i] + t[5];
        this.fx[i] = nx;
        this.fy[i] = ny;
        this.colorGroup[i] = m & 3;
        this.px[i] = this.tx[i] = nx * this.fScale + this.fOffsetX;
        this.py[i] = this.ty[i] = this.fOffsetY - ny * this.fScale;
      }
    } else {
      const att = this.attractor;
      for (let j = 0; j < jumps; j++) {
        const i = Math.floor(Math.random() * this.n);
        const [nx, ny] = stepStrange(att, this.fx[i], this.fy[i]);
        this.colorGroup[i] = flowGroup(nx - this.fx[i], ny - this.fy[i]);
        this.fx[i] = nx;
        this.fy[i] = ny;
        this.px[i] = this.tx[i] = nx * this.fScale + this.fOffsetX;
        this.py[i] = this.ty[i] = this.fOffsetY - ny * this.fScale;
      }
    }
  }

  private readonly frame = (now: number): void => {
    if (this.destroyed) return;
    this.rafId = requestAnimationFrame(this.frame);
    if (this.phase === "idle") return;

    const resting = this.phase === "framed";
    this.ctx.globalCompositeOperation = "source-over";
    this.ctx.fillStyle = withAlpha(
      this.colors.dark,
      resting ? 0.55 : this.phase === "fractal" ? 0.26 : 0.13,
    );
    this.ctx.fillRect(0, 0, this.width, this.height);

    const elapsed = now - this.phaseStart;

    if (this.phase === "burst") {
      for (let i = 0; i < this.n; i++) {
        this.px[i] += this.vx[i];
        this.py[i] += this.vy[i];
        this.vx[i] *= 0.945;
        this.vy[i] *= 0.945;
      }
      if (elapsed > BURST_MS) {
        if (this.heading === "frame") this.setFrameTargets(); // track live layout
        this.setPhase("converge");
      }
    } else if (this.phase === "converge") {
      const k = 0.004 + 0.1 * smoothstep(Math.min(1, elapsed / 1700));
      for (let i = 0; i < this.n; i++) {
        this.vx[i] = (this.vx[i] + (this.tx[i] - this.px[i]) * k) * 0.86;
        this.vy[i] = (this.vy[i] + (this.ty[i] - this.py[i]) * k) * 0.86;
        this.px[i] += this.vx[i];
        this.py[i] += this.vy[i];
      }
      if (elapsed > CONVERGE_MS) this.settle();
    } else if (this.phase === "fractal") {
      this.liveShimmer();
      if (elapsed > this.fractalHoldMs) {
        this.heading = "frame";
        this.setFrameTargets();
        this.beginBurst(this.width / 2, this.height / 2);
      }
    }

    this.draw();
  };

  private draw(): void {
    const ctx = this.ctx;
    if (this.heading === "fractal" && this.phase !== "burst") {
      // additive glow, colored by IFS map index / flow direction
      ctx.globalCompositeOperation = "lighter";
      const size = Math.max(1, Math.round(this.dpr * 1.4));
      for (let m = 0; m < 4; m++) {
        ctx.fillStyle = this.groupStyles[m];
        for (let i = 0; i < this.n; i++) {
          if (this.colorGroup[i] !== m) continue;
          ctx.fillRect(this.px[i], this.py[i], size, size);
        }
      }
      return;
    }
    // solid dust in the logo's own colors (burst transit and the frame)
    ctx.globalCompositeOperation = "source-over";
    const size = Math.max(2, Math.round(this.dpr * 1.7));
    let bucket = -1;
    for (let j = 0; j < this.n; j++) {
      const i = this.logoOrder[j];
      if (this.logoBucket[i] !== bucket) {
        bucket = this.logoBucket[i];
        ctx.fillStyle = this.logoBucketStyles[bucket];
      }
      ctx.fillRect(this.px[i], this.py[i], size, size);
    }
  }
}
