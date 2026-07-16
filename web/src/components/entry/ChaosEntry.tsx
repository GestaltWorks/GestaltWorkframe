"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChaosEngine, type ChaosColors, type EntryPhase } from "./chaosEngine";

export type ChaosEntryProps = {
  /** Logo image to particle-sample. Must be same-origin (or CORS-readable). */
  logoSrc: string;
  logoAlt: string;
  /** Uppercase label under the mark, e.g. the organization name. */
  label?: string;
  sublabel?: string;
  /** How long the fractal rests before the frame arrives. */
  fractalHoldMs?: number;
  /** Element the particle frame converges around; defaults to a centered box. */
  frameTargetRef?: React.RefObject<HTMLElement | null>;
  /** Palette override; defaults to the brand tokens in globals.css. */
  colors?: Partial<ChaosColors>;
  /** Fired on the click that starts the sequence. */
  onSequenceStart?: () => void;
  /** Fired when the particle frame has settled around the target. */
  onFramed?: () => void;
  /** Fired right after framing — reveal the real terminal now. */
  onReady?: () => void;
};

const READY_PAUSE_MS = 150;

function defaultFrameRect(): DOMRect {
  const width = Math.min(window.innerWidth * 0.74, 880);
  const height = Math.min(window.innerHeight * 0.62, 480);
  return new DOMRect((window.innerWidth - width) / 2, (window.innerHeight - height) / 2, width, height);
}

/** Read the brand palette from CSS custom properties, with hex fallbacks. */
function readBrandColors(): Partial<ChaosColors> {
  const style = getComputedStyle(document.documentElement);
  const read = (name: string): string | undefined => {
    const value = style.getPropertyValue(name).trim();
    return /^#[0-9a-fA-F]{6}$/.test(value) ? value : undefined;
  };
  return {
    dark: read("--color-brand-dark"),
    gold: read("--color-brand-gold"),
    goldWarm: read("--color-brand-gold-warm"),
    sage: read("--color-brand-sage"),
    offwhite: read("--color-brand-offwhite"),
  };
}

/**
 * Entry theater for the terminal: the logo mark assembles from particle dust,
 * idles alive, and on click bursts into a random fractal attractor, then its
 * particles converge into a frame around the terminal panel. Purely
 * presentational; the parent owns the terminal reveal (including any boot
 * theater typed inside the real terminal transcript).
 */
export default function ChaosEntry({
  logoSrc,
  logoAlt,
  label,
  sublabel,
  fractalHoldMs,
  frameTargetRef,
  colors,
  onSequenceStart,
  onFramed,
  onReady,
}: ChaosEntryProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const logoRef = useRef<HTMLImageElement>(null);
  const engineRef = useRef<ChaosEngine | null>(null);
  const readyTimerRef = useRef<number | null>(null);
  const [stage, setStage] = useState<"mark" | "running" | "framed" | "handoff">("mark");
  const [particlesLive, setParticlesLive] = useState(false);
  const [attractorName, setAttractorName] = useState("");

  const handlePhase = useCallback(
    (phase: EntryPhase, name: string) => {
      setAttractorName(name);
      if (phase === "logo") setParticlesLive(true);
      if (phase === "framed") {
        setStage("framed");
        onFramed?.();
        readyTimerRef.current = window.setTimeout(() => {
          onReady?.();
          setStage("handoff");
        }, READY_PAUSE_MS);
      }
    },
    [onFramed, onReady],
  );

  // assemble the mark from particle dust as soon as the logo pixels arrive
  useEffect(() => {
    const canvas = canvasRef.current;
    const logo = logoRef.current;
    if (!canvas || !logo) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let cancelled = false;
    const start = () => {
      if (cancelled || engineRef.current) return;
      try {
        const engine = new ChaosEngine({
          canvas,
          colors: { ...readBrandColors(), ...colors },
          fractalHoldMs,
          onPhase: handlePhase,
        });
        engine.assemble(
          logo,
          () => logo.getBoundingClientRect(),
          () => frameTargetRef?.current?.getBoundingClientRect() ?? defaultFrameRect(),
        );
        engineRef.current = engine;
      } catch {
        /* unreadable logo pixels: the static mark stays and clicks skip the theater */
      }
    };

    if (logo.complete && logo.naturalWidth > 0) start();
    else logo.addEventListener("load", start);
    return () => {
      cancelled = true;
      logo.removeEventListener("load", start);
    };
    // engine lifetime is mount-scoped; options are read once at ignition
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ignite = () => {
    if (stage !== "mark") return;
    if (engineRef.current?.burstToFractal()) {
      onSequenceStart?.();
      setStage("running");
      return;
    }
    // reduced motion or no engine: reveal without theater
    onSequenceStart?.();
    onFramed?.();
    onReady?.();
    setStage("handoff");
  };

  // Any click during the theater skips ahead — the animation is a flourish,
  // never a gate in front of the terminal.
  const skipAhead = () => {
    if (stage === "running") {
      engineRef.current?.finishNow(); // fires the framed phase immediately
      return;
    }
    if (stage === "framed") {
      if (readyTimerRef.current) window.clearTimeout(readyTimerRef.current);
      onReady?.();
      setStage("handoff");
    }
  };

  useEffect(() => {
    return () => {
      engineRef.current?.destroy();
      if (readyTimerRef.current) window.clearTimeout(readyTimerRef.current);
    };
  }, []);

  // Everything here is page-anchored: the canvas and overlays scroll away with
  // the entry section rather than sitting fixed over the whole viewport.
  return (
    <div
      ref={wrapperRef}
      className={`absolute inset-0 ${stage === "running" || stage === "framed" ? "cursor-pointer" : ""}`}
      onClick={skipAhead}
    >
      <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 z-0 h-full w-full" aria-hidden="true" />

      <div
        className={`absolute inset-0 z-10 flex items-center justify-center transition-opacity duration-300 ${
          stage === "mark" ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <div className="flex flex-col items-center gap-6">
          <button
            type="button"
            onClick={ignite}
            className="group outline-none transition-transform duration-300 hover:scale-[1.03] focus-visible:ring-2 focus-visible:ring-brand-gold"
            aria-label="Open terminal"
          >
            {/* Sampled at natural resolution; fades out once particles assemble. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              ref={logoRef}
              src={logoSrc}
              alt={logoAlt}
              draggable={false}
              className={`h-52 w-auto transition-opacity duration-500 sm:h-64 ${particlesLive ? "opacity-0" : "opacity-100"}`}
            />
          </button>
          {label ? (
            <div className="text-center">
              <div className="font-rajdhani text-sm uppercase tracking-[0.36em] text-brand-gold-warm/80">{label}</div>
              {sublabel ? (
                <div className="mt-3 font-mono text-xs uppercase tracking-[0.28em] text-brand-offwhite/42">{sublabel}</div>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      <div aria-live="polite" className="sr-only">
        {stage === "running" && attractorName ? `Rendering ${attractorName}.` : ""}
        {stage === "framed" || stage === "handoff" ? "Terminal is loading." : ""}
      </div>

      {stage === "running" && attractorName ? (
        <div className="pointer-events-none absolute bottom-6 left-1/2 z-10 -translate-x-1/2 font-mono text-[10px] uppercase tracking-[0.28em] text-brand-gold-warm/55">
          {attractorName} &middot; click to skip
        </div>
      ) : null}
    </div>
  );
}
