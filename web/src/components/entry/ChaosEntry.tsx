"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChaosEngine, type ChaosColors, type EntryPhase } from "./chaosEngine";

export type ChaosEntryProps = {
  /** Logo image to particle-sample. Must be same-origin (or CORS-readable). */
  logoSrc: string;
  logoAlt: string;
  /** Uppercase label under the mark, e.g. the deployment's terminal name. */
  label?: string;
  sublabel?: string;
  /** Lines typed inside the frame once it settles. */
  bootLines?: string[];
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
  /** Fired when boot lines finish typing — reveal the real terminal now. */
  onReady?: () => void;
};

const TYPE_INTERVAL_MS = 22;
const READY_PAUSE_MS = 400;

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
 * Entry theater for the guided terminal: the deployment's logo mark bursts
 * into a random fractal attractor, holds, then its particles converge into a
 * frame around the terminal panel while boot lines type. Purely presentational
 * — the parent owns when the real terminal mounts and becomes interactive.
 */
export default function ChaosEntry({
  logoSrc,
  logoAlt,
  label,
  sublabel,
  bootLines = [],
  fractalHoldMs,
  frameTargetRef,
  colors,
  onSequenceStart,
  onFramed,
  onReady,
}: ChaosEntryProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const logoRef = useRef<HTMLImageElement>(null);
  const engineRef = useRef<ChaosEngine | null>(null);
  const typeTimerRef = useRef<number | null>(null);
  const readyTimerRef = useRef<number | null>(null);
  const [stage, setStage] = useState<"mark" | "running" | "framed">("mark");
  const [attractorName, setAttractorName] = useState("");
  const [typedText, setTypedText] = useState("");
  const [frameBox, setFrameBox] = useState<DOMRect | null>(null);

  const finishSoon = useCallback(() => {
    readyTimerRef.current = window.setTimeout(() => onReady?.(), READY_PAUSE_MS);
  }, [onReady]);

  const typeBootLines = useCallback(() => {
    if (bootLines.length === 0) {
      finishSoon();
      return;
    }
    let line = 0;
    let char = 0;
    let out = "";
    typeTimerRef.current = window.setInterval(() => {
      if (line >= bootLines.length) {
        if (typeTimerRef.current) window.clearInterval(typeTimerRef.current);
        finishSoon();
        return;
      }
      const current = bootLines[line];
      if (char < current.length) {
        out += current[char++];
      } else {
        line++;
        char = 0;
        if (line < bootLines.length) out += "\n";
      }
      setTypedText(out);
    }, TYPE_INTERVAL_MS);
  }, [bootLines, finishSoon]);

  const handlePhase = useCallback(
    (phase: EntryPhase, name: string) => {
      setAttractorName(name);
      if (phase === "framed") {
        setStage("framed");
        setFrameBox(frameTargetRef?.current?.getBoundingClientRect() ?? defaultFrameRect());
        onFramed?.();
        typeBootLines();
      }
    },
    [frameTargetRef, onFramed, typeBootLines],
  );

  const ignite = () => {
    if (stage !== "mark") return;
    onSequenceStart?.();

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const canvas = canvasRef.current;
    const logo = logoRef.current;
    if (reducedMotion || !canvas || !logo || !logo.complete || logo.naturalWidth === 0) {
      onFramed?.();
      onReady?.();
      return;
    }

    try {
      const engine = new ChaosEngine({
        canvas,
        colors: { ...readBrandColors(), ...colors },
        fractalHoldMs,
        onPhase: handlePhase,
      });
      engine.ignite(logo, logo.getBoundingClientRect(), () =>
        frameTargetRef?.current?.getBoundingClientRect() ?? defaultFrameRect(),
      );
      engineRef.current = engine;
      setStage("running");
    } catch {
      // unreadable logo pixels (taint) or no canvas: reveal without theater
      onFramed?.();
      onReady?.();
    }
  };

  useEffect(() => {
    return () => {
      engineRef.current?.destroy();
      if (typeTimerRef.current) window.clearInterval(typeTimerRef.current);
      if (readyTimerRef.current) window.clearTimeout(readyTimerRef.current);
    };
  }, []);

  return (
    <div className="absolute inset-0">
      <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" aria-hidden="true" />

      <div
        className={`absolute inset-0 flex items-center justify-center transition-opacity duration-300 ${
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
            {/* Sampled at natural resolution on click; plain img keeps pixels readable. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img ref={logoRef} src={logoSrc} alt={logoAlt} className="h-52 w-auto sm:h-64" draggable={false} />
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
        {stage === "framed" ? "Terminal is loading." : ""}
      </div>

      {stage === "running" && attractorName ? (
        <div className="pointer-events-none absolute bottom-6 left-1/2 -translate-x-1/2 font-mono text-[10px] uppercase tracking-[0.28em] text-brand-gold-warm/55">
          {attractorName}
        </div>
      ) : null}

      {stage === "framed" && frameBox ? (
        <div
          className="pointer-events-none absolute overflow-hidden bg-brand-dark/85"
          style={{ left: frameBox.left, top: frameBox.top, width: frameBox.width, height: frameBox.height }}
        >
          <div className="flex items-center gap-2 border-b border-brand-gold-warm/20 px-4 py-2.5">
            <span className="h-2 w-2 rounded-full bg-brand-gold" />
            <span className="h-2 w-2 rounded-full bg-brand-gold-warm" />
            <span className="h-2 w-2 rounded-full bg-brand-sage" />
            {label ? (
              <span className="ml-2 font-mono text-[10px] uppercase tracking-[0.2em] text-brand-gold-warm/50">{label}</span>
            ) : null}
          </div>
          <div className="whitespace-pre-wrap p-5 font-mono text-xs leading-7 text-brand-gold-warm sm:text-sm">
            {typedText}
            <span className="ml-0.5 inline-block w-[0.6em] animate-pulse bg-brand-gold">&nbsp;</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
