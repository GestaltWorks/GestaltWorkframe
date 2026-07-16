"use client";

import { useState, useRef, useEffect, useId } from "react";
import { Terminal, Send, User, Bot, Loader2 } from "lucide-react";
import { apiUrl } from "@/lib/api";
import type { ProviderStatus } from "@/lib/api-types";
import type {
  ConnectionStatus,
  EntryStage,
  IntakeAnswers,
  IntakeQuestion,
  Message,
  Mode,
  ProviderHealth,
} from "./chat/types";
import {
  anthillFillBottom,
  anthillFillHeight,
  connectionStatusClass,
  connectionStatusLabel,
  createMessageId,
  createTerminalSessionId,
  defaultIntakeQuestions,
  defaultModes,
  emptyIntake,
  entryBootLines,
  objectiveOptionLabels,
  openingLines,
} from "./chat/constants";
import ChaosEntry from "./entry/ChaosEntry";
import { fetchDeploymentConfig, type PublicDeploymentConfig } from "@/lib/deploymentConfig";

export default function ChatWidget() {
  const widgetId = useId().replace(/:/g, "");
  const anthillGoldFillId = `${widgetId}-anthillGoldFill`;
  const anthillFillClipId = `${widgetId}-anthillFillClip`;
  const terminalHeadingId = `${widgetId}-terminal-heading`;
  const statusPanelId = `${widgetId}-status-panel`;
  const statusSummaryId = `${widgetId}-status-summary`;
  const intakeQuestionId = `${widgetId}-intake-question`;
  const intakeInputId = `${widgetId}-intake-input`;
  const chatInputId = `${widgetId}-chat-input`;
  const chatLogId = `${widgetId}-chat-log`;
  const [entryStage, setEntryStage] = useState<EntryStage>("intro");
  const [infoScrollProgress, setInfoScrollProgress] = useState(0);
  const [modes, setModes] = useState<Mode[]>(defaultModes);
  const [selectedMode, setSelectedMode] = useState<string>("pipeline");
  const [intakeQuestions, setIntakeQuestions] = useState<IntakeQuestion[]>(defaultIntakeQuestions);
  const [intakeAnswers, setIntakeAnswers] = useState<IntakeAnswers>(emptyIntake);
  const [intakeStep, setIntakeStep] = useState(0);
  const [intakeComplete, setIntakeComplete] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("checking");
  const [offlineReason, setOfflineReason] = useState("Checking backend health.");
  const [providerHealth, setProviderHealth] = useState<ProviderHealth | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [terminalSessionId] = useState(createTerminalSessionId);
  const [showStatusInfo, setShowStatusInfo] = useState(false);
  const [deployment, setDeployment] = useState<PublicDeploymentConfig | null>(null);

  const messagesScrollRef = useRef<HTMLDivElement>(null);
  const terminalPanelRef = useRef<HTMLDivElement>(null);
  const terminalFrameRef = useRef<HTMLElement | null>(null);
  const entryTimerRef = useRef<number | null>(null);
  const providerModels = providerHealth?.models || [providerHealth?.primary, providerHealth?.secondary];
  const callableProviders = providerModels.filter(
    (provider): provider is ProviderStatus => Boolean(provider?.configured && provider.callable)
  );
  const canChat = intakeComplete && connectionStatus === "online" && callableProviders.length > 0;
  const readyModelCount = callableProviders.length;
  const statusSummary = (() => {
    if (connectionStatus === "checking") return "Checking the terminal service.";
    if (connectionStatus === "online") return "The terminal is ready to respond.";
    if (providerHealth) return "The terminal service answered, but response models are not ready yet.";
    return "The terminal service is not reachable from this page.";
  })();
  const screenReaderStatus = `${statusSummary} ${readyModelCount} response model${readyModelCount === 1 ? "" : "s"} ready.`;
  const splittingEntry = entryStage === "splitting";
  const terminalVisible = entryStage === "done";
  const entryComplete = entryStage === "done";
  // The chaos entry is an opt-in deployment capability: it runs only when the
  // deployment's copy bundle sets `entry.style: chaos` AND provides a logo.
  // Every other deployment keeps the classic entry and terminal presentation.
  const entryConfig = (deployment?.copy?.entry ?? null) as { style?: string; boot_lines?: unknown } | null;
  const entryLogoSrc =
    entryConfig?.style === "chaos" ? deployment?.brand.logo_path || "" : "";
  const configuredBootLines = Array.isArray(entryConfig?.boot_lines)
    && entryConfig.boot_lines.every((line): line is string => typeof line === "string")
    ? entryConfig.boot_lines
    : entryBootLines;

  useEffect(() => {
    const updateInfoScrollProgress = () => {
      const progress = Math.min(Math.max(window.scrollY / Math.max(window.innerHeight * 0.75, 1), 0), 1);
      setInfoScrollProgress((current) => (Math.abs(current - progress) < 0.01 ? current : progress));
    };

    updateInfoScrollProgress();
    window.addEventListener("scroll", updateInfoScrollProgress, { passive: true });
    return () => {
      if (entryTimerRef.current) window.clearTimeout(entryTimerRef.current);
      window.removeEventListener("scroll", updateInfoScrollProgress);
    };
  }, []);

  const activateTerminal = () => {
    if (entryStage !== "intro") return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion) {
      setEntryStage("done");
      return;
    }
    setEntryStage("splitting");
    entryTimerRef.current = window.setTimeout(() => setEntryStage("done"), 2400);
  };

  const scrollToInfoContent = () => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    document.getElementById("site-info-heading")?.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  };
  
  useEffect(() => {
    let cancelled = false;

    const fetchJsonIfOk = async <T,>(path: string): Promise<T | null> => {
      const response = await fetch(apiUrl(path), { cache: "no-store" });
      return response.ok ? response.json() : null;
    };

    const loadServerState = async () => {
      try {
        const health = await fetch(apiUrl("/health/providers"), { cache: "no-store" });
        if (!health.ok) throw new Error(`Provider health endpoint returned ${health.status}`);
        const providerData = await health.json() as ProviderHealth;
        if (cancelled) return;
        setProviderHealth(providerData);
        const readyProviders = (providerData.models || [providerData.primary, providerData.secondary]).filter(
          (provider) => provider.configured && provider.callable
        );
        if (readyProviders.length > 0) {
          setConnectionStatus("online");
          setOfflineReason("");
        } else {
          setConnectionStatus("offline");
          setOfflineReason("The terminal service answered, but no model is ready to respond.");
        }

        const [modesData, intakeData] = await Promise.all([
          fetchJsonIfOk<{ modes?: Mode[] }>("/modes"),
          fetchJsonIfOk<{ questions?: IntakeQuestion[] }>("/intake/questions"),
        ]);

        if (cancelled) return;
        if (modesData?.modes) setModes(modesData.modes);
        if (intakeData?.questions) setIntakeQuestions(intakeData.questions);
      } catch (error) {
        if (!cancelled) {
          setConnectionStatus("offline");
          setProviderHealth(null);
          const reason = error instanceof Error ? error.message : "Backend health check failed";
          setOfflineReason(
            reason.includes("404")
              ? "The terminal service is not reachable from this page."
              : reason
          );
        }
      }
    };

    loadServerState();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchDeploymentConfig()
      .then((config) => {
        if (!cancelled) setDeployment(config);
      })
      .catch(() => {
        /* no deployment config: the plain entry animation is the fallback */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    messagesScrollRef.current?.scrollTo({
      top: messagesScrollRef.current.scrollHeight,
      behavior: reducedMotion ? "auto" : "smooth",
    });
  }, [messages, isTyping]);

  useEffect(() => {
    if (entryStage === "done") terminalPanelRef.current?.focus();
  }, [entryStage]);

  const sendChatTurn = async (
    promptText: string,
    userMessageId: string,
    assistantMessageId: string,
    overrides?: {
      // The intake-completion path fires the opening user turn before
      // React commits the new intake state, so the closure still sees
      // `intakeComplete=false`. The override lets that path pass the
      // freshly-resolved values explicitly while every other caller
      // keeps its current behavior.
      mode?: string;
      intakeAnswers?: IntakeAnswers;
      intakeComplete?: boolean;
    },
  ) => {
    setIsTyping(true);
    try {
      const response = await fetch(apiUrl("/chat/stream"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: promptText,
          mode: overrides?.mode ?? selectedMode,
          conversation_id: conversationId,
          terminal_session_id: terminalSessionId,
          intake_complete: overrides?.intakeComplete ?? intakeComplete,
          intake: overrides?.intakeAnswers ?? intakeAnswers,
        }),
      });

      if (!response.ok) {
        const fallback = response.status === 429
          ? "The terminal is receiving too much traffic. Please wait a bit and try again."
          : "Network response was not ok";
        const detail = await response.json().catch(() => null) as { detail?: string } | null;
        throw new Error(detail?.detail ?? fallback);
      }
      if (!response.body) throw new Error("No response body");

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      const handleStreamData = (dataStr: string) => {
        if (dataStr === "[DONE]") {
          setIsTyping(false);
          return;
        }
        try {
          const data = JSON.parse(dataStr) as {
            conversation_id?: string;
            selected_mode?: Mode["id"];
            content?: string;
            error?: string | { code?: string; message?: string; request_id?: string };
          };
          if (data.conversation_id && !conversationId) {
            setConversationId(data.conversation_id);
          }
          if (data.selected_mode) {
            setSelectedMode(data.selected_mode);
          }
          if (typeof data.content === "string") {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMessageId
                  ? { ...msg, content: msg.content + data.content }
                  : msg
              )
            );
          }
          if (data.error) {
            // The error envelope is either a plain string (older shape) or
            // `{code, message, request_id}` (current shape from api/chat.py).
            const errorMessage = typeof data.error === "string"
              ? data.error
              : data.error?.message || "The chat stream failed. Please try again.";
            // Set the error field instead of mutating content. The renderer
            // shows an error banner with a Retry action below the partial reply.
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMessageId
                  ? { ...msg, error: errorMessage, promptedByMessageId: userMessageId }
                  : msg
              )
            );
          }
        } catch (error) {
          console.error("Error parsing stream event", error);
        }
      };

      const processEventBlock = (block: string) => {
        // The FastAPI stream emits one JSON payload per SSE event. Multi-line
        // data blocks are joined for spec compatibility but are not expected here.
        const dataStr = block
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.replace(/^data: ?/, ""))
          .join("\n");
        if (dataStr) handleStreamData(dataStr);
      };

      while (true) {
        const { value, done } = await reader.read();
        if (value) buffer += decoder.decode(value, { stream: true });
        if (done) buffer += decoder.decode();

        const eventBlocks = buffer.split(/\r?\n\r?\n/);
        buffer = eventBlocks.pop() ?? "";
        eventBlocks.forEach(processEventBlock);

        if (done) break;
      }

      if (buffer.trim()) processEventBlock(buffer);
    } catch (error) {
      console.error("Error sending message:", error);
      const errorMessage = error instanceof Error ? error.message : "Failed to connect to server";
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === assistantMessageId
            ? { ...msg, error: errorMessage, promptedByMessageId: userMessageId }
            : msg
        )
      );
    } finally {
      setIsTyping(false);
    }
  };

  const handleSend = async () => {
    if (!input.trim() || !canChat) return;
    const userMessage: Message = { id: createMessageId(), role: "user", content: input };
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    const assistantMessageId = createMessageId();
    setMessages((prev) => [...prev, { id: assistantMessageId, role: "assistant", content: "", promptedByMessageId: userMessage.id }]);
    await sendChatTurn(userMessage.content, userMessage.id, assistantMessageId);
  };

  const handleRetry = (assistantMessageId: string) => {
    const target = messages.find((msg) => msg.id === assistantMessageId);
    if (!target?.promptedByMessageId) return;
    const userMessage = messages.find((msg) => msg.id === target.promptedByMessageId);
    if (!userMessage) return;
    // Clear the prior error and partial content so the retry stream writes into
    // a clean assistant bubble. The user message itself stays in place.
    setMessages((prev) =>
      prev.map((msg) =>
        msg.id === assistantMessageId ? { ...msg, content: "", error: undefined } : msg
      )
    );
    void sendChatTurn(userMessage.content, userMessage.id, assistantMessageId);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const currentQuestion = intakeQuestions[intakeStep];
  const currentModeName = modes.find((mode) => mode.id === selectedMode)?.name || "Guided Intake";
  const isObjectiveQuestion = currentQuestion?.id === "objective";

  // Maps help_needed intake answer strings to bot mode IDs.
  // Keys must stay in sync with the server-side option text for the help_needed question
  // in /intake/questions. A mismatch falls through to the "automator" default.
  const helpNeededModeMap: Record<string, string> = {
    "Help me choose the next step": "pipeline",
    "Give me a technical answer I can use": "automator",
    "Show me examples or patterns": "automator",
    "Walk me through it so I understand": "educator",
    "Service Inquiry": "pipeline",
    "Automator Assistance": "automator",
    "Automation Educator": "educator",
  };

  const inferModeFromIntake = (answers: IntakeAnswers): string => {
    const helpMode = helpNeededModeMap[answers.help_needed];
    if (helpMode) return helpMode;
    if (answers.objective.includes("support") || answers.objective.includes("consulting")) return "pipeline";
    if (answers.objective.includes("Learn")) return "educator";
    return "automator";
  };

  const intakeHandoffMessage = (answers: IntakeAnswers, mode: string) => {
    const modeName = modes.find((item) => item.id === mode)?.name || mode;
    return [
      "Ready.",
      `Focus: ${answers.building.trim()}`,
      `Path: ${modeName}`,
    ].join("\n");
  };

  const updateIntakeAnswer = (questionId: keyof IntakeAnswers, value: string) => {
    setIntakeAnswers((prev) => ({ ...prev, [questionId]: value }));
  };

  const captureIntake = async (answers: IntakeAnswers, mode: string) => {
    try {
      const response = await fetch(apiUrl("/intake/submissions"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          terminal_session_id: terminalSessionId,
          selected_mode: mode,
          intake: answers,
          source_path: typeof window !== "undefined" ? window.location.pathname : "/terminal",
        }),
      });
      if (!response.ok) throw new Error(`Intake capture returned ${response.status}`);
    } catch (error) {
      console.error("Error capturing intake:", error);
      setMessages((prev) => [
        ...prev,
        {
          id: createMessageId(),
          role: "system",
          content: "Intake capture could not be confirmed. You can still continue, and the next chat turn will retry the session link.",
        },
      ]);
    }
  };

  // Compose a first-message string from the intake answers so the
  // chat opens on a real turn instead of an empty cursor. Reads like
  // the user typed a short self-introduction and asked where to
  // start. Multi-line so the bot can parse the structure; the
  // /chat/stream handler also gets the raw intake payload in the
  // body for routing.
  const composeIntakeOpeningPrompt = (answers: IntakeAnswers): string => {
    const lines: string[] = [];
    if (answers.objective.trim()) {
      lines.push(`Goal: ${answers.objective.trim()}`);
    }
    if (answers.building.trim()) {
      lines.push(`What I'm working on: ${answers.building.trim()}`);
    }
    if (answers.maturity.trim()) {
      lines.push(`Where I am today: ${answers.maturity.trim()}`);
    }
    if (answers.help_needed.trim()) {
      lines.push(`What would help most: ${answers.help_needed.trim()}`);
    }
    lines.push("");
    lines.push("Where should we start?");
    return lines.join("\n");
  };

  const completeIntake = (answers: IntakeAnswers) => {
    const nextMode = inferModeFromIntake(answers);
    setSelectedMode(nextMode);
    setIntakeComplete(true);
    void captureIntake(answers, nextMode);

    if (connectionStatus !== "online") {
      // Offline: keep the previous behaviour. The handoff line plus
      // the offline note prevents a confusing "waiting" state.
      setMessages([
        {
          id: "intake-complete",
          role: "system",
          content: `Intake complete. The terminal UI is ready, but the server connection is offline. Reason: ${offlineReason}`,
        },
      ]);
      return;
    }

    // Auto-fire the first chat turn so the bot opens the conversation
    // with a context-aware reply instead of staring at the user.
    const handoffLine = intakeHandoffMessage(answers, nextMode);
    const openingPrompt = composeIntakeOpeningPrompt(answers);
    const openingUserMessage: Message = {
      id: createMessageId(),
      role: "user",
      content: openingPrompt,
    };
    const assistantMessageId = createMessageId();
    setMessages([
      { id: "intake-complete", role: "system", content: handoffLine },
      openingUserMessage,
      {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        promptedByMessageId: openingUserMessage.id,
      },
    ]);
    // The intake-complete state hasn't been committed yet; pass the
    // freshly resolved values through so the request reflects them.
    void sendChatTurn(openingPrompt, openingUserMessage.id, assistantMessageId, {
      mode: nextMode,
      intakeAnswers: answers,
      intakeComplete: true,
    });
  };

  const advanceIntake = (answers = intakeAnswers) => {
    if (!currentQuestion) return;
    const value = answers[currentQuestion.id].trim();
    if (!value) return;

    if (intakeStep < intakeQuestions.length - 1) {
      setIntakeStep((step) => step + 1);
      return;
    }

    completeIntake(answers);
  };

  const chooseIntakeOption = (questionId: keyof IntakeAnswers, value: string) => {
    const nextAnswers = { ...intakeAnswers, [questionId]: value };
    setIntakeAnswers(nextAnswers);
    advanceIntake(nextAnswers);
  };

  const goBackIntake = () => {
    setIntakeStep((step) => Math.max(0, step - 1));
  };

  return (
    <main id="main-content" aria-labelledby={terminalHeadingId} className="relative min-h-screen overflow-hidden bg-brand-dark px-4 py-6 text-brand-offwhite sm:px-8 lg:px-12">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(220,208,119,0.14),_transparent_34%),linear-gradient(135deg,_rgba(86,107,91,0.2),_transparent_42%)]" />
      <h1 id={terminalHeadingId} className="sr-only">Guided terminal</h1>
      <div
        aria-hidden={entryComplete}
        className={`absolute inset-0 z-30 flex items-center justify-center overflow-hidden bg-brand-dark transition-opacity duration-700 ${
          entryComplete ? "pointer-events-none" : ""
        } ${entryComplete && !entryLogoSrc ? "opacity-0" : "opacity-100"}`}
      >
        {entryLogoSrc ? (
          <ChaosEntry
            logoSrc={entryLogoSrc}
            logoAlt={`${deployment?.identity.short_name || "Terminal"} mark`}
            label="Guided terminal"
            sublabel="Click to open"
            frameLabel={`${deployment?.identity.short_name || "Guided"} — terminal`}
            bootLines={configuredBootLines}
            frameTargetRef={terminalFrameRef}
            onSequenceStart={() => setEntryStage("splitting")}
            onReady={() => setEntryStage("done")}
          />
        ) : (
          <>
            <div className={`absolute left-0 top-1/2 h-px w-[52vw] origin-left bg-gradient-to-r from-transparent via-brand-gold to-transparent transition-transform duration-[1800ms] ease-out ${splittingEntry ? "translate-x-[105vw] opacity-100" : "translate-x-[-54vw] opacity-0"}`} />
            <div className={`absolute right-0 top-[42%] h-px w-[42vw] origin-right bg-gradient-to-l from-transparent via-brand-gold-warm to-transparent transition-transform delay-200 duration-[1700ms] ease-out ${splittingEntry ? "-translate-x-[100vw] opacity-100" : "translate-x-[44vw] opacity-0"}`} />
            <div className={`absolute bottom-[30%] left-1/2 h-px w-[38vw] bg-gradient-to-r from-transparent via-brand-sage to-transparent transition-transform delay-300 duration-[1600ms] ease-out ${splittingEntry ? "translate-x-[40vw] opacity-80" : "-translate-x-1/2 opacity-0"}`} />

            <div className="relative flex flex-col items-center gap-6">
              <button
                type="button"
                onClick={activateTerminal}
                disabled={splittingEntry}
                className="group relative flex h-52 w-52 items-center justify-center rounded-full border border-brand-gold-warm/40 bg-black/30 outline-none transition-transform duration-300 hover:scale-[1.03] focus-visible:ring-2 focus-visible:ring-brand-gold sm:h-64 sm:w-64"
                aria-label="Open terminal"
              >
                <Terminal size={96} className="text-brand-gold" aria-hidden="true" />
              </button>
              <div className={`text-center transition-opacity duration-500 ${splittingEntry ? "opacity-0" : "opacity-100"}`}>
                <div className="font-rajdhani text-sm uppercase tracking-[0.36em] text-brand-gold-warm/80">Guided terminal</div>
                <div className="mt-3 font-mono text-xs uppercase tracking-[0.28em] text-brand-offwhite/42">Click to open</div>
              </div>
            </div>
          </>
        )}

        <button
          type="button"
          onClick={scrollToInfoContent}
          disabled={splittingEntry}
          className={`absolute bottom-6 left-1/2 flex -translate-x-1/2 items-end gap-3 text-brand-gold-warm outline-none transition-opacity duration-500 hover:text-brand-gold focus-visible:ring-2 focus-visible:ring-brand-gold ${entryStage !== "intro" ? "pointer-events-none opacity-0" : "opacity-80"}`}
          aria-label="Scroll to site information"
        >
          <span className="pb-3 font-mono text-[10px] uppercase tracking-[0.28em] text-brand-offwhite/35">Scroll</span>
          <span className="relative h-20 w-28" aria-hidden="true">
            <span className="absolute bottom-2 left-1/2 h-px w-28 -translate-x-1/2 bg-gradient-to-r from-transparent via-brand-gold-warm/40 to-transparent" />
            <svg viewBox="0 0 112 80" className="absolute inset-0 h-full w-full overflow-visible" fill="none">
              <defs>
                <linearGradient id={anthillGoldFillId} x1="56" y1="20" x2="56" y2="72" gradientUnits="userSpaceOnUse">
                  <stop stopColor="#DCD077" stopOpacity="0.9" />
                  <stop offset="0.55" stopColor="#D8C883" stopOpacity="0.72" />
                  <stop offset="1" stopColor="#D4BF91" stopOpacity="0.38" />
                </linearGradient>
                <clipPath id={anthillFillClipId}>
                  <rect
                    x="0"
                    y={anthillFillBottom - infoScrollProgress * anthillFillHeight}
                    width="112"
                    height={infoScrollProgress * anthillFillHeight}
                  />
                </clipPath>
              </defs>
              <path
                d="M15 70 C25 61 30 48 39 42 C46 37 46 26 55 21 C66 27 66 40 73 44 C83 49 88 61 98 70 Z"
                fill={`url(#${anthillGoldFillId})`}
                clipPath={`url(#${anthillFillClipId})`}
              />
              <path
                d="M15 70 C25 61 30 48 39 42 C46 37 46 26 55 21 C66 27 66 40 73 44 C83 49 88 61 98 70"
                stroke="currentColor"
                strokeWidth="2.6"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="opacity-90"
              />
              <path
                d="M24 68 C37 62 49 60 57 61 C68 61 78 63 91 68"
                stroke="currentColor"
                strokeWidth="1.2"
                strokeLinecap="round"
                className="opacity-28"
              />
            </svg>
          </span>
          <span className="pb-3 font-mono text-[10px] uppercase tracking-[0.28em] text-brand-offwhite/35">Down</span>
        </button>
      </div>
      <div className="sr-only" aria-live="polite">{entryComplete ? "Terminal is ready." : "Terminal is loading."}</div>
      <div id={statusSummaryId} className="sr-only" aria-live="polite">{screenReaderStatus}</div>

      {/* Mounted (hidden) as soon as the entry sequence starts so the panel
          hydrates behind the theater and the particle frame can measure it.
          Opacity-only reveal: a transform here would shift the framed rect. */}
      {entryStage !== "intro" && <div
        ref={terminalPanelRef}
        tabIndex={-1}
        aria-hidden={!entryComplete}
        className={`relative z-40 mx-auto flex min-h-[calc(100vh-3rem)] max-w-7xl flex-col gap-6 transition-opacity duration-700 ${terminalVisible ? "opacity-100" : "pointer-events-none opacity-0"}`}
      >
        {/* The chaos entry's particle frame carries the identity; the header
            would compete with it, so it only renders on the plain path. */}
        {!entryLogoSrc && (
          <header className="flex items-center justify-between gap-4 text-xs uppercase tracking-[0.28em] text-brand-gold-warm/80">
            <div className="flex items-center gap-3">
              <Terminal size={32} className="text-brand-gold" aria-hidden="true" />
              <div className="hidden sm:block">
                <div>Guided terminal</div>
              </div>
            </div>
            <div className="rounded-full border border-brand-gold-warm/30 px-4 py-2 text-brand-gold" aria-live="polite">
              {intakeComplete ? currentModeName : "Guided intake"}
            </div>
          </header>
        )}

        <section className="grid flex-1 gap-6 lg:grid-cols-[0.75fr_1.25fr]">
          <aside className={entryLogoSrc
            ? "flex flex-col justify-end p-6"
            : "flex flex-col justify-end rounded-3xl border border-brand-gold-warm/20 bg-black/20 p-6 shadow-2xl shadow-black/30 backdrop-blur"}>
            <p className="font-rajdhani text-sm uppercase tracking-[0.32em] text-brand-gold-warm/80">
              Guided terminal
            </p>
            <h2 className="mt-4 font-rajdhani text-4xl font-semibold leading-none text-brand-offwhite sm:text-5xl">
              Tell us what you are trying to do.
            </h2>
            <p className="mt-5 max-w-xl font-inter text-base leading-7 text-brand-offwhite/68">
              Start wherever it feels easiest. A few guided prompts help the terminal route the session. From there it
              can bring the right references, tools, and next steps into the conversation.
            </p>
            <p className="mt-5 max-w-xl border-t border-brand-gold-warm/15 pt-4 font-inter text-xs leading-6 text-brand-offwhite/45">
              Privacy note: we only store intake answers, basic request metadata, and the chat you send so the service
              can respond, route the session, improve reliability, and protect the service.
              <a href="/privacy" className="text-brand-gold-warm/80 hover:text-brand-gold"> Full policy</a>.
            </p>
          </aside>

          <section
            ref={terminalFrameRef}
            className={`flex min-h-[620px] flex-col overflow-hidden font-mono text-sm ${entryLogoSrc
              ? "bg-[#1c1a20]/95"
              : "rounded-3xl border border-brand-gold-warm/25 bg-[#0d0c10]/95 shadow-2xl shadow-black/40"}`}
            aria-label="Guided terminal session"
            aria-describedby={statusSummaryId}
          >
            {/* Chaos path: title bar matches the boot overlay it crossfades with. */}
            <div className={`flex items-center justify-between border-b border-brand-gold-warm/20 ${entryLogoSrc ? "px-4 py-2.5" : "bg-white/[0.03] px-5 py-4"}`}>
              {entryLogoSrc ? (
                <div className="flex items-center gap-2">
                  <span className="h-2 w-2 rounded-full bg-brand-gold" />
                  <span className="h-2 w-2 rounded-full bg-brand-gold-warm" />
                  <span className="h-2 w-2 rounded-full bg-brand-sage" />
                  <span className="ml-2 text-[10px] uppercase tracking-[0.2em] text-brand-gold-warm/50" translate="no">
                    {deployment?.identity.short_name || "Guided"} — terminal
                  </span>
                </div>
              ) : (
                <div className="flex items-center gap-3 text-brand-gold">
                  <Terminal size={18} aria-hidden="true" />
                  <span className="font-semibold tracking-[0.18em]" translate="no">TERMINAL</span>
                </div>
              )}
              <div className="flex items-center gap-3">
                {entryLogoSrc && (
                  <span className="text-[10px] uppercase tracking-[0.2em] text-brand-gold/80" aria-live="polite">
                    {intakeComplete ? currentModeName : "Guided intake"}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => setShowStatusInfo((value) => !value)}
                  className="rounded-full px-2 py-1 text-xs tracking-[0.2em] text-brand-offwhite/70 transition-colors hover:text-brand-gold"
                  aria-expanded={showStatusInfo}
                  aria-controls={statusPanelId}
                >
                  Status: <span className={connectionStatusClass[connectionStatus]}>{connectionStatusLabel[connectionStatus]}</span>
                </button>
              </div>
            </div>

            <div
              ref={messagesScrollRef}
              role="region"
              aria-label="Terminal interaction area"
              className="flex-1 overflow-y-auto p-5 sm:p-7"
            >
              {showStatusInfo && (
                <div id={statusPanelId} className="mb-6 rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 text-xs text-brand-offwhite/72">
                  <div className="uppercase tracking-[0.22em] text-brand-offwhite/38">Terminal status</div>
                  <div className={`mt-1 ${connectionStatusClass[connectionStatus]}`}>{connectionStatusLabel[connectionStatus]}</div>
                  <div className="mt-2">{statusSummary}</div>
                  <div className="mt-1 text-brand-offwhite/42">
                    Response capacity: {readyModelCount > 0 ? "available" : "waiting on model connection"}
                  </div>
                </div>
              )}

              <div className="space-y-1 text-brand-offwhite/86">
                {openingLines.map((line, index) => (
                  <div key={`${line}-${index}`} className={line ? "" : "h-4"}>
                    {line && <span className="text-brand-gold-warm">&gt;</span>} {line}
                  </div>
                ))}
              </div>

              {!intakeComplete && currentQuestion && (
                <form className="mt-8 space-y-5 text-brand-offwhite/86" onSubmit={(event) => { event.preventDefault(); advanceIntake(); }}>
                  <div className="text-xs uppercase tracking-[0.28em] text-brand-gold-warm">
                    Intake {intakeStep + 1}/{intakeQuestions.length}
                  </div>
                  <div id={intakeQuestionId} className="text-lg font-semibold leading-snug text-brand-offwhite">
                    <span className="text-brand-gold-warm">&gt;</span> {currentQuestion.label}
                  </div>

                  {currentQuestion.options ? (
                    <div className="grid gap-3" role="group" aria-labelledby={intakeQuestionId}>
                      {currentQuestion.options.map((option, index) => (
                        <button
                          type="button"
                          key={option}
                          onClick={() => chooseIntakeOption(currentQuestion.id, option)}
                          className="group w-full rounded-2xl border border-brand-gold-warm/20 bg-white/[0.045] px-4 py-3 text-left text-sm text-brand-offwhite transition-colors hover:border-brand-gold hover:text-brand-gold"
                        >
                          <span className="mr-3 text-brand-gold-warm" aria-hidden="true">[{index + 1}]</span>
                          {isObjectiveQuestion ? objectiveOptionLabels[option] || option : option}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="space-y-3">
                      <textarea
                        id={intakeInputId}
                        value={intakeAnswers[currentQuestion.id]}
                        onChange={(e) => updateIntakeAnswer(currentQuestion.id, e.target.value)}
                        placeholder={currentQuestion.placeholder}
                        aria-labelledby={intakeQuestionId}
                        className="min-h-32 w-full rounded-2xl border border-brand-gold-warm/25 bg-black/35 p-4 text-sm text-brand-offwhite outline-none placeholder:text-brand-offwhite/45 focus:border-brand-gold"
                        rows={4}
                      />
                      <button
                        type="submit"
                        disabled={!intakeAnswers[currentQuestion.id].trim()}
                        className="rounded-full bg-brand-gold px-5 py-2 text-sm font-semibold text-brand-dark transition-colors hover:bg-brand-gold-mid disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        Continue
                      </button>
                    </div>
                  )}

                  {intakeStep > 0 && (
                    <button type="button" onClick={goBackIntake} className="rounded-full px-2 py-1 text-xs text-brand-offwhite/70 hover:text-brand-offwhite">
                      Back
                    </button>
                  )}
                </form>
              )}

              {intakeComplete && messages.length === 0 && (
                <div className="mt-8 text-brand-offwhite/45">
                  &gt; Initialize session ({modes.find((mode) => mode.id === selectedMode)?.name || selectedMode})...
                  <br />&gt; Ready for input.
                </div>
              )}

              <div
                id={chatLogId}
                role="log"
                aria-live="polite"
                aria-relevant="additions text"
                aria-busy={isTyping}
                aria-label="Terminal transcript"
                className="mt-8 space-y-5"
              >
                {messages.map((msg) => (
                  <div key={msg.id} className={`flex flex-col gap-2 ${msg.role === "user" ? "items-end" : ""}`}>
                    <div className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : ""}`}>
                      <div className={`mt-1 flex-shrink-0 ${msg.role === "user" ? "text-brand-gold" : "text-brand-sage"}`} aria-hidden="true">
                        {msg.role === "user" ? <User size={16} /> : <Bot size={16} />}
                      </div>
                      <div className={`max-w-[85%] whitespace-pre-wrap ${msg.role === "user" ? "text-brand-gold-warm" : "text-brand-offwhite/86"}`}>
                        <span className="sr-only">{msg.role} message: </span>
                        {msg.content}
                      </div>
                    </div>
                    {msg.error && (
                      <div className="ml-7 max-w-[85%] rounded-xl border border-red-400/35 bg-red-950/25 p-3 text-xs text-red-100" role="alert">
                        <p className="font-semibold">Error: {msg.error}</p>
                        {msg.promptedByMessageId && (
                          <button
                            type="button"
                            onClick={() => handleRetry(msg.id)}
                            disabled={isTyping}
                            className="mt-2 rounded-full border border-red-300/40 px-3 py-1 text-xs font-semibold text-red-100 transition-colors hover:border-red-200 hover:text-red-50 disabled:cursor-wait disabled:opacity-50"
                          >
                            Retry
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                ))}

                {isTyping && (
                  <div className="flex gap-3 text-brand-gold" role="status" aria-label="Assistant is responding">
                    <Bot size={16} className="mt-1" aria-hidden="true" />
                    <Loader2 size={16} className="mt-1 animate-spin" aria-hidden="true" />
                  </div>
                )}
              </div>

            </div>

            <form className="flex gap-3 border-t border-brand-gold-warm/20 bg-black/25 p-4" onSubmit={(event) => { event.preventDefault(); void handleSend(); }}>
              <label htmlFor={chatInputId} className="sr-only">Terminal command</label>
              <span className="mt-2 text-brand-gold-warm" aria-hidden="true">&gt;</span>
              <textarea
                id={chatInputId}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={!intakeComplete ? "Complete intake first..." : canChat ? "Enter command..." : "No callable model connection..."}
                aria-describedby={statusSummaryId}
                className="flex-1 resize-none bg-transparent py-2 text-brand-offwhite outline-none placeholder:text-brand-offwhite/45"
                disabled={!canChat}
                rows={1}
              />
              <button
                type="submit"
                disabled={!input.trim() || isTyping || !canChat}
                className="rounded-full p-2 text-brand-gold-warm transition-colors hover:bg-white/5 hover:text-brand-gold disabled:cursor-not-allowed disabled:opacity-35"
                aria-label="Send command"
              >
                <Send size={18} aria-hidden="true" />
              </button>
            </form>
          </section>
        </section>
      </div>}
    </main>
  );
}
