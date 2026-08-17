import {
  useCallback,
  useEffect,
  forwardRef,
  useMemo,
  useState,
  type Dispatch,
  type FormEvent,
  type ReactNode,
  type SetStateAction,
} from "react";
import {
  Activity,
  ArrowUpCircle,
  ArrowUpDown,
  Bot,
  Brain,
  Check,
  CircleAlert,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Cloud,
  Cpu,
  Database,
  Eye,
  EyeOff,
  ExternalLink,
  Gem,
  Globe2,
  Grid3X3,
  HardDrive,
  Hexagon,
  ImageIcon,
  Layers,
  Loader2,
  LogOut,
  MessageSquareText,
  Mic,
  Moon,
  PauseCircle,
  PlayCircle,
  Plus,
  Orbit,
  Palette,
  Pencil,
  RotateCcw,
  Search,
  Server,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Triangle,
  Waves,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { WechatBridge } from "@/components/wechat/WechatBridge";
import { SkillsCatalogSettings } from "@/components/settings/SkillsCatalogSettings";
import { CapabilitiesView } from "@/components/settings/CapabilitiesView";
import { TokenUsageHeatmap } from "@/components/settings/TokenUsageHeatmap";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  checkVersion,
  createModelConfiguration,
  fetchAutomations,
  fetchChannels,
  fetchSettings,
  fetchSettingsUsage,
  fetchProviderModels,
  runAutomationAction,
  selfUpdate,
  updateAutomation,
  updateChannelSettings,
  updateImageGenerationSettings,
  updateModelConfiguration,
  updateNetworkSafetySettings,
  updateProviderSettings,
  updateScreenshotSettings,
  updateSettings,
  updateSystemIoSettings,
  updateTranscriptionSettings,
  updateTtsSettings,
  updateVideoGenerationSettings,
  updateWebSearchSettings,
} from "@/lib/api";
import type { ChannelRow, ChannelsPayload } from "@/lib/api";
import { getHostApi } from "@/lib/runtime";
import { fmtDateTime, relativeTime } from "@/lib/format";
import {
  providerBrand,
  providerDisplayLabel,
} from "@/lib/provider-brand";
import { cn } from "@/lib/utils";
import { shortWorkspacePath } from "@/lib/workspace";
import { useClient } from "@/providers/ClientProvider";
import type {
  AutomationsPayload,
  AutomationUpdatePayload,
  GuardLevel,
  ImageGenerationSettingsUpdate,
  NetworkSafetySettingsUpdate,
  ProviderModelsPayload,
  ScreenshotSettingsUpdate,
  SessionAutomationJob,
  SettingsPayload,
  SystemIoSettingsUpdate,
  SkillSummary,
  TranscriptionSettingsUpdate,
  TtsSettingsUpdate,
  VideoGenerationSettingsUpdate,
  WebSearchSettingsUpdate,
  WebuiDefaultAccessMode,
} from "@/lib/types";

export type SettingsSectionKey =
  | "overview"
  | "appearance"
  | "models"
  | "providers"
  | "image"
  | "video"
  | "vision"
  | "voice"
  | "tts"
  | "browser"
  | "capabilities"
  | "automations"
  | "skills"
  | "runtime"
  | "systemIo"
  | "channels"
  | "advanced";

type LocalDensity = "comfortable" | "compact";
type LocalActivityMode = "auto" | "expanded";
type AutomationFilter = "all" | "active" | "paused" | "failed" | "system";
type AutomationSort = "next" | "last" | "updated" | "name";
type AutomationAction = "enable" | "disable" | "delete" | "run";

interface LocalPreferences {
  density: LocalDensity;
  activityMode: LocalActivityMode;
  codeWrap: boolean;
  brandLogos: boolean;
}

interface AgentSettingsDraft {
  model: string;
  provider: string;
  modelPreset: string;
  presetLabel: string;
  contextWindowTokens: number;
  timezone: string;
  botName: string;
  botIcon: string;
  toolHintMaxLength: number;
}

interface ModelConfigurationDraft {
  label: string;
  provider: string;
  model: string;
}

type PendingRestartSection = "runtime" | "browser" | "image" | "video" | "vision" | "systemIo";
type PendingRestartSections = Record<PendingRestartSection, boolean>;
type RestartAwarePayload = {
  requires_restart?: boolean;
  surface?: SettingsPayload["surface"];
  runtime_surface?: SettingsPayload["runtime_surface"];
  runtime_capabilities?: SettingsPayload["runtime_capabilities"];
};
const CONTEXT_WINDOW_TOKEN_OPTIONS = [65_536, 262_144] as const;
const DEFERRED_MODEL_LIST_PROVIDERS = new Set([
  "ollama",
]);
const DEFERRED_MODEL_LIST_QUERY_MIN_LENGTH = 2;

const FALLBACK_TIMEZONES = [
  "UTC",
  "Asia/Shanghai",
  "Asia/Hong_Kong",
  "Asia/Tokyo",
  "Asia/Seoul",
  "Asia/Singapore",
  "Asia/Taipei",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Amsterdam",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Toronto",
  "America/Sao_Paulo",
  "Australia/Sydney",
  "Pacific/Auckland",
];

const LOCAL_PREFS_STORAGE_KEY = "biscuitbot-webui.settings-preferences";

const DEFAULT_LOCAL_PREFS: LocalPreferences = {
  density: "comfortable",
  activityMode: "auto",
  codeWrap: true,
  brandLogos: true,
};

const LOCAL_UNCONFIGURED_PROVIDER_ORDER = new Map(
  ["vllm", "ollama", "lm_studio", "atomic_chat", "ovms"].map((name, index) => [
    name,
    index,
  ]),
);

const IMAGE_ASPECT_RATIO_OPTIONS = ["1:1", "3:4", "9:16", "4:3", "16:9", "3:2", "2:3", "21:9"];
const IMAGE_SIZE_OPTIONS = ["1K", "2K", "4K", "1024x1024", "1536x1024", "1024x1536"];
const VIDEO_RATIO_OPTIONS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"];
const VIDEO_RESOLUTION_OPTIONS = ["480p", "720p", "1080p", "4K"];
const SEEDANCE_MODELS = ["doubao-seedance-2-0-260128", "doubao-seedance-2-5-260628"];
const EMPTY_PENDING_RESTART_SECTIONS: PendingRestartSections = {
  runtime: false,
  browser: false,
  image: false,
  video: false,
  vision: false,
  systemIo: false,
};

interface SettingsViewProps {
  theme: "light" | "dark";
  initialSection?: SettingsSectionKey;
  initialSettings?: SettingsPayload | null;
  showSidebar?: boolean;
  onToggleTheme: () => void;
  onBackToChat: () => void;
  onModelNameChange: (modelName: string | null) => void;
  onSettingsChange?: (payload: SettingsPayload) => void;
  skills?: SkillSummary[];
  onSkillsDeleted?: () => void;
  onWorkspaceSettingsChange?: () => void | Promise<void>;
  onSectionChange?: (section: SettingsSectionKey) => void;
  onLogout?: () => void;
  onRestart?: () => void;
  onNativeEngineRestart?: () => Promise<string>;
  isRestarting?: boolean;
  hostChromeInset?: boolean;
  /** 桌面壳（native）下隐藏「退出登录」，与顶栏 padding 解耦（浏览器桌面端仍显示）。 */
  hideLogout?: boolean;
}

function readLocalPreferences(): LocalPreferences {
  try {
    const raw = window.localStorage.getItem(LOCAL_PREFS_STORAGE_KEY);
    if (!raw) return DEFAULT_LOCAL_PREFS;
    const parsed = JSON.parse(raw) as Partial<LocalPreferences>;
    return {
      density: parsed.density === "compact" ? "compact" : "comfortable",
      activityMode: parsed.activityMode === "expanded" ? "expanded" : "auto",
      codeWrap: parsed.codeWrap !== false,
      brandLogos: parsed.brandLogos !== false,
    };
  } catch {
    return DEFAULT_LOCAL_PREFS;
  }
}

function modelPresetValue(payload: SettingsPayload): string {
  return payload.agent.model_preset || "default";
}

function defaultPreset(payload: SettingsPayload): SettingsPayload["model_presets"][number] | null {
  return payload.model_presets.find((preset) => preset.is_default) ?? null;
}

function normalizeContextWindowTokens(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 65_536;
}

function editableDefaultProvider(payload: SettingsPayload): string {
  const base = defaultPreset(payload);
  return base?.provider ?? payload.agent.provider ?? payload.agent.resolved_provider ?? "";
}

function settingsProviderRow(
  payload: SettingsPayload,
  provider: string | null | undefined,
): SettingsPayload["providers"][number] | null {
  if (!provider) return null;
  return payload.providers.find((row) => row.name === provider) ?? null;
}

function settingsProviderConfigured(
  payload: SettingsPayload,
  provider: string | null | undefined,
): boolean {
  const row = settingsProviderRow(payload, provider);
  if (row) return row.configured;
  if (provider === "auto") {
    const resolvedRow = settingsProviderRow(
      payload,
      payload.agent.resolved_provider ?? payload.agent.provider,
    );
    if (resolvedRow) return resolvedRow.configured;
  }
  return payload.agent.has_api_key;
}

function providersWithCapability(
  settings: SettingsPayload,
  capability: string,
): SettingsPayload["providers"] {
  return settings.providers.filter((provider) =>
    provider.capabilities?.includes(capability),
  );
}

const DEFAULT_AGENT_SETTINGS_DRAFT: AgentSettingsDraft = {
  model: "",
  provider: "",
  modelPreset: "default",
  presetLabel: "Default",
  contextWindowTokens: 65_536,
  timezone: "UTC",
  botName: "biscuitbot",
  botIcon: "",
  toolHintMaxLength: 40,
};

const DEFAULT_WEB_SEARCH_FORM: WebSearchSettingsUpdate = {
  provider: "duckduckgo",
  apiKey: "",
  baseUrl: "",
  maxResults: 5,
  timeout: 30,
  useJinaReader: true,
};

const DEFAULT_IMAGE_GENERATION_FORM: ImageGenerationSettingsUpdate = {
  enabled: false,
  provider: "dashscope",
  model: "wanx2.1-t2i-turbo",
  defaultAspectRatio: "1:1",
  defaultImageSize: "1K",
  maxImagesPerTurn: 4,
};

const DEFAULT_VIDEO_GENERATION_FORM: VideoGenerationSettingsUpdate = {
  enabled: false,
  model: "doubao-seedance-2-5-260628",
  defaultRatio: "16:9",
  defaultDuration: 5,
  defaultResolution: "",
  generateAudio: false,
  watermark: true,
};

const DEFAULT_SCREENSHOT_FORM: ScreenshotSettingsUpdate = {
  enabled: false,
  visionModel: null,
  visionModelOverride: null,
  maxWidth: 1920,
  maxHeight: 1080,
  quality: 85,
};

const DEFAULT_SYSTEM_IO_FORM: SystemIoSettingsUpdate = {
  enabled: false,
  allowActions: [],
};

const DEFAULT_TRANSCRIPTION_FORM: TranscriptionSettingsUpdate = {
  enabled: true,
  provider: "dashscope",
  model: "",
  language: "",
  maxDurationSec: 120,
  maxUploadMb: 25,
};

const DEFAULT_TRANSCRIPTION_SETTINGS: NonNullable<SettingsPayload["transcription"]> = {
  enabled: true,
  provider: "dashscope",
  provider_configured: false,
  model: "paraformer-v2",
  language: null,
  max_duration_sec: 120,
  max_upload_mb: 25,
};

const DEFAULT_TTS_FORM: TtsSettingsUpdate = {
  enabled: true,
  provider: "edge-tts",
  model: "",
  voice: "",
  rate: "",
};

const DEFAULT_TTS_SETTINGS: NonNullable<SettingsPayload["tts"]> = {
  enabled: true,
  provider: "edge-tts",
  provider_configured: true,
  model: "",
  voice: "zh-CN-XiaoxiaoNeural",
  rate: null,
  save_dir: "generated/tts",
};

const DEFAULT_NETWORK_SAFETY_FORM: NetworkSafetySettingsUpdate = {
  webuiAllowLocalServiceAccess: true,
  webuiDefaultAccessMode: "default",
  guardLevel: "standard",
  coldStorageDays: 14,
  duplicateSimilarityThreshold: 0.6,
};

function agentDraftFromPayload(payload: SettingsPayload): AgentSettingsDraft {
  const fallbackDefault = defaultPreset(payload);
  const activePresetName = modelPresetValue(payload);
  const activePreset =
    payload.model_presets.find((preset) => preset.name === activePresetName) ?? fallbackDefault;
  return {
    model: activePreset?.model ?? payload.agent.model,
    provider: activePreset?.is_default
      ? editableDefaultProvider(payload)
      : activePreset?.provider ?? editableDefaultProvider(payload),
    modelPreset: activePresetName,
    presetLabel: activePreset?.label ?? activePresetName,
    contextWindowTokens: normalizeContextWindowTokens(
      activePreset?.context_window_tokens ?? payload.agent.context_window_tokens,
    ),
    timezone: payload.agent.timezone,
    botName: payload.agent.bot_name,
    botIcon: payload.agent.bot_icon,
    toolHintMaxLength: payload.agent.tool_hint_max_length,
  };
}

function webSearchFormFromPayload(
  payload: SettingsPayload,
  previous?: WebSearchSettingsUpdate,
): WebSearchSettingsUpdate {
  return {
    provider: payload.web_search.provider,
    apiKey: previous?.provider === payload.web_search.provider ? previous.apiKey ?? "" : "",
    baseUrl: payload.web_search.base_url ?? "",
    maxResults: payload.web_search.max_results,
    timeout: payload.web_search.timeout,
    useJinaReader: payload.web.fetch.use_jina_reader,
  };
}

function imageGenerationFormFromPayload(payload: SettingsPayload): ImageGenerationSettingsUpdate {
  return {
    enabled: payload.image_generation.enabled,
    provider: payload.image_generation.provider,
    model: payload.image_generation.model,
    defaultAspectRatio: payload.image_generation.default_aspect_ratio,
    defaultImageSize: payload.image_generation.default_image_size,
    maxImagesPerTurn: payload.image_generation.max_images_per_turn,
  };
}

function videoGenerationFormFromPayload(payload: SettingsPayload): VideoGenerationSettingsUpdate {
  return {
    enabled: payload.video_generation.enabled,
    model: payload.video_generation.model,
    defaultRatio: payload.video_generation.default_ratio,
    defaultDuration: payload.video_generation.default_duration,
    defaultResolution: payload.video_generation.default_resolution ?? "",
    generateAudio: payload.video_generation.generate_audio,
    watermark: payload.video_generation.watermark,
  };
}

function screenshotFormFromPayload(payload: SettingsPayload): ScreenshotSettingsUpdate {
  return {
    enabled: payload.screenshot.enabled,
    visionModel: payload.screenshot.vision_model,
    visionModelOverride: payload.screenshot.vision_model_override,
    maxWidth: payload.screenshot.max_width,
    maxHeight: payload.screenshot.max_height,
    quality: payload.screenshot.quality,
  };
}

function systemIoFormFromPayload(payload: SettingsPayload): SystemIoSettingsUpdate {
  return {
    enabled: payload.system_io.enabled,
    allowActions: [...payload.system_io.allow_actions],
  };
}

function transcriptionFormFromPayload(payload: SettingsPayload): TranscriptionSettingsUpdate {
  const transcription = payload.transcription ?? DEFAULT_TRANSCRIPTION_SETTINGS;
  return {
    enabled: transcription.enabled,
    provider: transcription.provider,
    model: transcription.model,
    language: transcription.language ?? "",
    maxDurationSec: transcription.max_duration_sec,
    maxUploadMb: transcription.max_upload_mb,
  };
}

function ttsFormFromPayload(payload: SettingsPayload): TtsSettingsUpdate {
  const tts = payload.tts ?? DEFAULT_TTS_SETTINGS;
  return {
    enabled: tts.enabled,
    provider: tts.provider,
    model: tts.model,
    voice: tts.voice,
    rate: tts.rate ?? "",
  };
}

function networkSafetyFormFromPayload(payload: SettingsPayload): NetworkSafetySettingsUpdate {
  const rawGuardLevel = payload.advanced.guard_level ?? "standard";
  const guardLevel: GuardLevel =
    rawGuardLevel === "minimal" || rawGuardLevel === "off" ? rawGuardLevel : "standard";
  return {
    webuiAllowLocalServiceAccess:
      payload.advanced.webui_allow_local_service_access ??
      payload.advanced.allow_local_preview_access ??
      true,
    webuiDefaultAccessMode: visibleWebuiDefaultAccessMode(
      payload.advanced.webui_default_access_mode,
    ),
    guardLevel,
    coldStorageDays: payload.advanced.cold_storage_days ?? 14,
    duplicateSimilarityThreshold: payload.advanced.duplicate_similarity_threshold ?? 0.6,
  };
}

function pendingRestartSectionsFromPayload(payload: SettingsPayload): PendingRestartSections {
  const sections = payload.restart_required_sections ?? [];
  return {
    runtime: sections.includes("runtime"),
    browser: sections.includes("browser"),
    image: sections.includes("image"),
    video: sections.includes("video"),
    vision: sections.includes("vision"),
    systemIo: sections.includes("systemIo"),
  };
}

export function SettingsView({
  theme,
  initialSection = "overview",
  initialSettings = null,
  showSidebar = true,
  onToggleTheme,
  onBackToChat,
  onModelNameChange,
  onSettingsChange,
  skills = [],
  onSkillsDeleted,
  onWorkspaceSettingsChange,
  onSectionChange,
  onLogout,
  onRestart,
  onNativeEngineRestart,
  isRestarting = false,
  hostChromeInset = false,
  hideLogout = false,
}: SettingsViewProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [settings, setSettings] = useState<SettingsPayload | null>(() => initialSettings);
  const [automations, setAutomations] = useState<AutomationsPayload | null>(null);
  const [loading, setLoading] = useState(() => initialSettings === null);
  const [automationsLoading, setAutomationsLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modelConfigurationOpen, setModelConfigurationOpen] = useState(false);
  const [modelConfigurationSaving, setModelConfigurationSaving] = useState(false);
  const [modelConfigurationForm, setModelConfigurationForm] = useState<ModelConfigurationDraft>({
    label: "",
    provider: "",
    model: "",
  });
  const [webSearchSaving, setWebSearchSaving] = useState(false);
  const [imageGenerationSaving, setImageGenerationSaving] = useState(false);
  const [videoGenerationSaving, setVideoGenerationSaving] = useState(false);
  const [transcriptionSaving, setTranscriptionSaving] = useState(false);
  const [ttsSaving, setTtsSaving] = useState(false);
  const [networkSafetySaving, setNetworkSafetySaving] = useState(false);
  const [screenshotSaving, setScreenshotSaving] = useState(false);
  const [systemIoSaving, setSystemIoSaving] = useState(false);
  const [hostEngineApplying, setHostEngineApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSectionKey>(initialSection);
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [providerQuery, setProviderQuery] = useState("");
  const [automationsQuery, setAutomationsQuery] = useState("");
  const [automationsFilter, setAutomationsFilter] = useState<AutomationFilter>("all");
  const [automationsSort, setAutomationsSort] = useState<AutomationSort>("next");
  const [automationsError, setAutomationsError] = useState<string | null>(null);
  const [automationAction, setAutomationAction] = useState<string | null>(null);
  const [automationPendingDelete, setAutomationPendingDelete] =
    useState<SessionAutomationJob | null>(null);
  const [automationPendingEdit, setAutomationPendingEdit] =
    useState<SessionAutomationJob | null>(null);
  const [pendingRestartSections, setPendingRestartSections] = useState<PendingRestartSections>(
    EMPTY_PENDING_RESTART_SECTIONS,
  );
  const [localPrefs, setLocalPrefs] = useState<LocalPreferences>(() => readLocalPreferences());
  const [webSearchForm, setWebSearchForm] = useState<WebSearchSettingsUpdate>(() =>
    initialSettings ? webSearchFormFromPayload(initialSettings) : DEFAULT_WEB_SEARCH_FORM,
  );
  const [imageGenerationForm, setImageGenerationForm] = useState<ImageGenerationSettingsUpdate>(
    () =>
      initialSettings
        ? imageGenerationFormFromPayload(initialSettings)
        : DEFAULT_IMAGE_GENERATION_FORM,
  );
  const [videoGenerationForm, setVideoGenerationForm] = useState<VideoGenerationSettingsUpdate>(
    () =>
      initialSettings
        ? videoGenerationFormFromPayload(initialSettings)
        : DEFAULT_VIDEO_GENERATION_FORM,
  );
  const [screenshotForm, setScreenshotForm] = useState<ScreenshotSettingsUpdate>(() =>
    initialSettings ? screenshotFormFromPayload(initialSettings) : DEFAULT_SCREENSHOT_FORM,
  );
  const [systemIoForm, setSystemIoForm] = useState<SystemIoSettingsUpdate>(() =>
    initialSettings ? systemIoFormFromPayload(initialSettings) : DEFAULT_SYSTEM_IO_FORM,
  );
  const [transcriptionForm, setTranscriptionForm] = useState<TranscriptionSettingsUpdate>(
    () => initialSettings ? transcriptionFormFromPayload(initialSettings) : DEFAULT_TRANSCRIPTION_FORM,
  );
  const [ttsForm, setTtsForm] = useState<TtsSettingsUpdate>(
    () => initialSettings ? ttsFormFromPayload(initialSettings) : DEFAULT_TTS_FORM,
  );
  const [networkSafetyForm, setNetworkSafetyForm] = useState<NetworkSafetySettingsUpdate>(() =>
    initialSettings ? networkSafetyFormFromPayload(initialSettings) : DEFAULT_NETWORK_SAFETY_FORM,
  );

  useEffect(() => {
    setActiveSection(initialSection);
  }, [initialSection]);

  const selectSection = useCallback(
    (section: SettingsSectionKey) => {
      setActiveSection(section);
      onSectionChange?.(section);
    },
    [onSectionChange],
  );
  const [webSearchKeyVisible, setWebSearchKeyVisible] = useState(false);
  const [webSearchKeyEditing, setWebSearchKeyEditing] = useState(false);
  const [form, setForm] = useState<AgentSettingsDraft>(() =>
    initialSettings ? agentDraftFromPayload(initialSettings) : DEFAULT_AGENT_SETTINGS_DRAFT,
  );

  const text = useCallback(
    (key: string, fallback: string, options?: Record<string, unknown>) =>
      t(key, { defaultValue: fallback, ...(options ?? {}) }),
    [t],
  );

  const applyPayload = useCallback((payload: SettingsPayload) => {
    setSettings(payload);
    setForm(agentDraftFromPayload(payload));
    setWebSearchForm((prev) => webSearchFormFromPayload(payload, prev));
    setImageGenerationForm(imageGenerationFormFromPayload(payload));
    setVideoGenerationForm(videoGenerationFormFromPayload(payload));
    setScreenshotForm(screenshotFormFromPayload(payload));
    setSystemIoForm(systemIoFormFromPayload(payload));
    setTranscriptionForm(transcriptionFormFromPayload(payload));
    setTtsForm(ttsFormFromPayload(payload));
    setNetworkSafetyForm(networkSafetyFormFromPayload(payload));
    if (payload.restart_required_sections) {
      setPendingRestartSections(pendingRestartSectionsFromPayload(payload));
    }
    onSettingsChange?.(payload);
  }, [onSettingsChange]);

  useEffect(() => {
    if (!initialSettings || settings !== null) return;
    applyPayload(initialSettings);
    setLoading(false);
  }, [applyPayload, initialSettings, settings]);

  useEffect(() => {
    let cancelled = false;
    const showLoading = settings === null;
    if (showLoading) setLoading(true);
    fetchSettings(token)
      .then((payload) => {
        if (!cancelled) {
          applyPayload(payload);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled && showLoading) setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applyPayload, token]);

  const hasSettings = settings !== null;
  useEffect(() => {
    if (activeSection !== "overview" || !hasSettings) return;
    let cancelled = false;
    const refresh = () => {
      fetchSettingsUsage(token)
        .then((usage) => {
          if (cancelled) return;
          setSettings((current) => (current ? { ...current, usage } : current));
        })
        .catch(() => {});
    };
    void refresh();
    const interval = window.setInterval(refresh, 5000);
    const onFocus = () => refresh();
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [activeSection, hasSettings, token]);

  const refreshAutomations = useCallback(
    async (showLoading = false) => {
      if (showLoading) setAutomationsLoading(true);
      try {
        const payload = await fetchAutomations(token);
        setAutomations(payload);
        setAutomationsError(null);
      } catch (err) {
        setAutomationsError((err as Error).message);
      } finally {
        if (showLoading) setAutomationsLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    if (activeSection !== "automations") return;
    let cancelled = false;
    const refresh = async (showLoading = false) => {
      if (cancelled) return;
      if (showLoading) setAutomationsLoading(true);
      try {
        const payload = await fetchAutomations(token);
        if (cancelled) return;
        setAutomations(payload);
        setAutomationsError(null);
      } catch (err) {
        if (!cancelled) setAutomationsError((err as Error).message);
      } finally {
        if (!cancelled && showLoading) setAutomationsLoading(false);
      }
    };
    void refresh(true);
    const interval = window.setInterval(() => void refresh(false), 5000);
    const refreshOnFocus = () => {
      if (document.visibilityState !== "hidden") void refresh(false);
    };
    window.addEventListener("focus", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnFocus);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnFocus);
    };
  }, [activeSection, token]);

  useEffect(() => {
    try {
      window.localStorage.setItem(LOCAL_PREFS_STORAGE_KEY, JSON.stringify(localPrefs));
    } catch {
      // Browser-only preferences should never block settings.
    }
  }, [localPrefs]);

  const modelDirty = useMemo(() => {
    if (!settings) return false;
    const activePresetName = modelPresetValue(settings);
    const selectedPreset = settings.model_presets.find((preset) => preset.name === form.modelPreset);
    if (!selectedPreset) return form.modelPreset !== activePresetName;
    const selectedProvider = selectedPreset.is_default
      ? editableDefaultProvider(settings)
      : selectedPreset.provider;
    return (
      form.modelPreset !== activePresetName ||
      form.model !== selectedPreset.model ||
      form.provider !== selectedProvider ||
      form.contextWindowTokens !== normalizeContextWindowTokens(selectedPreset.context_window_tokens) ||
      (!selectedPreset.is_default && form.presetLabel.trim() !== selectedPreset.label)
    );
  }, [form, settings]);

  const runtimeDirty = useMemo(() => {
    if (!settings) return false;
    return (
      form.timezone !== settings.agent.timezone ||
      form.botName !== settings.agent.bot_name ||
      form.botIcon !== settings.agent.bot_icon
    );
  }, [form, settings]);

  const imageGenerationDirty = useMemo(() => {
    if (!settings) return false;
    return (
      imageGenerationForm.enabled !== settings.image_generation.enabled ||
      imageGenerationForm.provider !== settings.image_generation.provider ||
      imageGenerationForm.model !== settings.image_generation.model ||
      imageGenerationForm.defaultAspectRatio !== settings.image_generation.default_aspect_ratio ||
      imageGenerationForm.defaultImageSize !== settings.image_generation.default_image_size ||
      imageGenerationForm.maxImagesPerTurn !== settings.image_generation.max_images_per_turn
    );
  }, [imageGenerationForm, settings]);

  const videoGenerationDirty = useMemo(() => {
    if (!settings) return false;
    return (
      videoGenerationForm.enabled !== settings.video_generation.enabled ||
      videoGenerationForm.model !== settings.video_generation.model ||
      videoGenerationForm.defaultRatio !== settings.video_generation.default_ratio ||
      videoGenerationForm.defaultDuration !== settings.video_generation.default_duration ||
      videoGenerationForm.defaultResolution !==
        (settings.video_generation.default_resolution ?? "") ||
      videoGenerationForm.generateAudio !== settings.video_generation.generate_audio ||
      videoGenerationForm.watermark !== settings.video_generation.watermark
    );
  }, [videoGenerationForm, settings]);

  const screenshotDirty = useMemo(() => {
    if (!settings) return false;
    return (
      screenshotForm.enabled !== settings.screenshot.enabled ||
      screenshotForm.visionModel !== settings.screenshot.vision_model ||
      screenshotForm.visionModelOverride !== settings.screenshot.vision_model_override ||
      screenshotForm.maxWidth !== settings.screenshot.max_width ||
      screenshotForm.maxHeight !== settings.screenshot.max_height ||
      screenshotForm.quality !== settings.screenshot.quality
    );
  }, [screenshotForm, settings]);

  const systemIoDirty = useMemo(() => {
    if (!settings) return false;
    const current = [...settings.system_io.allow_actions].sort().join(",");
    const form = [...systemIoForm.allowActions].sort().join(",");
    return (
      systemIoForm.enabled !== settings.system_io.enabled || current !== form
    );
  }, [systemIoForm, settings]);

  const transcriptionDirty = useMemo(() => {
    if (!settings) return false;
    const transcription = settings.transcription ?? DEFAULT_TRANSCRIPTION_SETTINGS;
    return (
      transcriptionForm.enabled !== transcription.enabled ||
      transcriptionForm.provider !== transcription.provider ||
      transcriptionForm.model !== transcription.model ||
      transcriptionForm.language !== (transcription.language ?? "") ||
      transcriptionForm.maxDurationSec !== transcription.max_duration_sec ||
      transcriptionForm.maxUploadMb !== transcription.max_upload_mb
    );
  }, [settings, transcriptionForm]);

  const ttsDirty = useMemo(() => {
    if (!settings) return false;
    const tts = settings.tts ?? DEFAULT_TTS_SETTINGS;
    return (
      ttsForm.enabled !== tts.enabled ||
      ttsForm.provider !== tts.provider ||
      ttsForm.model !== tts.model ||
      ttsForm.voice !== tts.voice ||
      ttsForm.rate !== (tts.rate ?? "")
    );
  }, [settings, ttsForm]);

  const networkSafetyDirty = useMemo(() => {
    if (!settings) return false;
    const currentLocalServiceAccess =
      settings.advanced.webui_allow_local_service_access ?? settings.advanced.allow_local_preview_access ?? true;
    const currentDefaultAccess = visibleWebuiDefaultAccessMode(settings.advanced.webui_default_access_mode);
    const currentGuardLevel = settings.advanced.guard_level ?? "standard";
    const currentColdStorageDays = settings.advanced.cold_storage_days ?? 14;
    const currentDuplicateSimilarityThreshold =
      settings.advanced.duplicate_similarity_threshold ?? 0.6;
    return (
      networkSafetyForm.webuiAllowLocalServiceAccess !== currentLocalServiceAccess ||
      networkSafetyForm.webuiDefaultAccessMode !== currentDefaultAccess ||
      networkSafetyForm.guardLevel !== currentGuardLevel ||
      networkSafetyForm.coldStorageDays !== currentColdStorageDays ||
      networkSafetyForm.duplicateSimilarityThreshold !== currentDuplicateSimilarityThreshold
    );
  }, [networkSafetyForm, settings]);

  const configuredModelProviderOptions = useMemo(
    () =>
      settings?.providers
        .filter((provider) => provider.configured && provider.model_selectable !== false)
        .map((provider) => ({ name: provider.name, label: provider.label })) ?? [],
    [settings],
  );

  const hasPendingRestart = useMemo(
    () =>
      !!settings?.requires_restart ||
      pendingRestartSections.runtime ||
      pendingRestartSections.browser ||
      pendingRestartSections.image,
    [pendingRestartSections, settings?.requires_restart],
  );

  const restartViaSettingsSurface = useCallback(async () => {
    const isNativeHost = (settings?.surface ?? settings?.runtime_surface) === "native";
    if (
      isNativeHost &&
      settings?.runtime_capabilities?.can_restart_engine &&
      onNativeEngineRestart
    ) {
      setHostEngineApplying(true);
      try {
        const nextToken = await onNativeEngineRestart();
        const payload = await fetchSettings(nextToken);
        applyPayload(payload);
        setPendingRestartSections(EMPTY_PENDING_RESTART_SECTIONS);
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setHostEngineApplying(false);
      }
      return;
    }
    onRestart?.();
  }, [applyPayload, onNativeEngineRestart, onRestart, settings]);

  const maybeRestartHostEngine = useCallback(
    async (payload: RestartAwarePayload) => {
      const surface = payload.surface ?? payload.runtime_surface ?? settings?.surface ?? settings?.runtime_surface;
      const capabilities = payload.runtime_capabilities ?? settings?.runtime_capabilities;
      const isNativeHost = surface === "native";
      if (
        !payload.requires_restart ||
        !isNativeHost ||
        !capabilities?.can_restart_engine ||
        !onNativeEngineRestart
      ) {
        return;
      }
      setHostEngineApplying(true);
      try {
        const nextToken = await onNativeEngineRestart();
        const refreshed = await fetchSettings(nextToken);
        applyPayload(refreshed);
        setPendingRestartSections(EMPTY_PENDING_RESTART_SECTIONS);
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setHostEngineApplying(false);
      }
    },
    [applyPayload, onNativeEngineRestart, settings],
  );

  const saveModelSettings = async () => {
    if (!settings || !modelDirty || saving) return;
    setSaving(true);
    try {
      const selectedPreset = settings.model_presets.find((preset) => preset.name === form.modelPreset);
      let payload: SettingsPayload;
      if (selectedPreset && !selectedPreset.is_default) {
        payload = await updateModelConfiguration(token, {
          name: selectedPreset.name,
          label: form.presetLabel.trim(),
          model: form.model,
          provider: form.provider,
          ...(form.contextWindowTokens !== selectedPreset.context_window_tokens
            ? { contextWindowTokens: form.contextWindowTokens }
            : {}),
        });
      } else {
        const defaultModel = defaultPreset(settings)?.model ?? settings.agent.model;
        const defaultProvider = editableDefaultProvider(settings);
        const defaultContextWindowTokens = normalizeContextWindowTokens(
          defaultPreset(settings)?.context_window_tokens ?? settings.agent.context_window_tokens,
        );
        payload = await updateSettings(token, {
          modelPreset: form.modelPreset,
          ...(form.model !== defaultModel ? { model: form.model } : {}),
          ...(form.provider !== defaultProvider ? { provider: form.provider } : {}),
          ...(form.contextWindowTokens !== defaultContextWindowTokens
            ? { contextWindowTokens: form.contextWindowTokens }
            : {}),
        });
      }
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const openModelConfigurationDialog = () => {
    if (!settings) return;
    const currentProvider = settings.agent.provider;
    const provider =
      configuredModelProviderOptions.find((option) => option.name === currentProvider)?.name ??
      configuredModelProviderOptions[0]?.name ??
      "";
    setModelConfigurationForm({
      label: "",
      provider,
      model: "",
    });
    setModelConfigurationOpen(true);
  };

  const handleCreateModelConfiguration = async () => {
    if (modelConfigurationSaving) return;
    const label = modelConfigurationForm.label.trim();
    const provider = modelConfigurationForm.provider.trim();
    const model = modelConfigurationForm.model.trim();
    if (!label || !provider || !model) return;
    setModelConfigurationSaving(true);
    try {
      const payload = await createModelConfiguration(token, {
        label,
        provider,
        model,
      });
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setModelConfigurationOpen(false);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setModelConfigurationSaving(false);
    }
  };

  const saveRuntimeSettings = async () => {
    if (!settings || !runtimeDirty || saving) return;
    setSaving(true);
    try {
      const payload = await updateSettings(token, {
        timezone: form.timezone,
        botName: form.botName,
        botIcon: form.botIcon,
      });
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await onWorkspaceSettingsChange?.();
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const saveImageGenerationSettings = async () => {
    if (!settings || !imageGenerationDirty || imageGenerationSaving) return;
    setImageGenerationSaving(true);
    try {
      const payload = await updateImageGenerationSettings(token, imageGenerationForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, image: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setImageGenerationSaving(false);
    }
  };

  const saveVideoGenerationSettings = async () => {
    if (!settings || !videoGenerationDirty || videoGenerationSaving) return;
    setVideoGenerationSaving(true);
    try {
      const payload = await updateVideoGenerationSettings(token, videoGenerationForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, video: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setVideoGenerationSaving(false);
    }
  };

  const saveScreenshotSettings = async () => {
    if (!settings || !screenshotDirty || screenshotSaving) return;
    setScreenshotSaving(true);
    try {
      const payload = await updateScreenshotSettings(token, screenshotForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, vision: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setScreenshotSaving(false);
    }
  };

  const saveSystemIoSettings = async () => {
    if (!settings || !systemIoDirty || systemIoSaving) return;
    setSystemIoSaving(true);
    try {
      const payload = await updateSystemIoSettings(token, systemIoForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, systemIo: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSystemIoSaving(false);
    }
  };

  const saveTranscriptionSettings = async () => {
    if (!settings || !transcriptionDirty || transcriptionSaving) return;
    setTranscriptionSaving(true);
    try {
      const payload = await updateTranscriptionSettings(token, transcriptionForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, browser: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setTranscriptionSaving(false);
    }
  };

  const saveTtsSettings = async () => {
    if (!settings || !ttsDirty || ttsSaving) return;
    setTtsSaving(true);
    try {
      const payload = await updateTtsSettings(token, ttsForm);
      applyPayload(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setTtsSaving(false);
    }
  };

  const saveNetworkSafetySettings = async () => {
    if (!settings || !networkSafetyDirty || networkSafetySaving) return;
    setNetworkSafetySaving(true);
    try {
      const payload = await updateNetworkSafetySettings(token, networkSafetyForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setNetworkSafetySaving(false);
    }
  };

  const saveWebSearch = async () => {
    if (!settings || webSearchSaving) return;
    const provider = settings.web_search.providers.find((item) => item.name === webSearchForm.provider);
    if (!provider) return;
    const apiKey = webSearchForm.apiKey?.trim() ?? "";
    const baseUrl = webSearchForm.baseUrl?.trim() ?? "";
    const hasExistingSecret =
      provider.credential === "api_key" &&
      webSearchForm.provider === settings.web_search.provider &&
      !!settings.web_search.api_key_hint;

    if (provider.credential === "api_key" && !apiKey && !hasExistingSecret) {
      setError(t("settings.byok.webSearch.apiKeyRequired"));
      return;
    }
    if (provider.credential === "base_url" && !baseUrl) {
      setError(t("settings.byok.webSearch.baseUrlRequired"));
      return;
    }

    setWebSearchSaving(true);
    try {
      const webFetchRestartRequired =
        (webSearchForm.useJinaReader ?? settings.web.fetch.use_jina_reader) !==
        settings.web.fetch.use_jina_reader;
      const update: WebSearchSettingsUpdate = {
        provider: webSearchForm.provider,
        maxResults: webSearchForm.maxResults,
        timeout: webSearchForm.timeout,
        useJinaReader: webSearchForm.useJinaReader,
      };
      if (provider.credential === "api_key" && apiKey) update.apiKey = apiKey;
      if (provider.credential === "base_url") update.baseUrl = baseUrl;
      const payload = await updateWebSearchSettings(token, update);
      applyPayload(payload);
      if (payload.requires_restart || webFetchRestartRequired) {
        setPendingRestartSections((prev) => ({ ...prev, browser: true }));
      }
      await maybeRestartHostEngine(payload);
      setWebSearchForm((prev) => ({
        provider: payload.web_search.provider,
        apiKey: "",
        baseUrl: payload.web_search.base_url ?? prev.baseUrl ?? "",
        maxResults: payload.web_search.max_results,
        timeout: payload.web_search.timeout,
        useJinaReader: payload.web.fetch.use_jina_reader,
      }));
      setWebSearchKeyVisible(false);
      setWebSearchKeyEditing(false);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setWebSearchSaving(false);
    }
  };

  const handleToggleProvider = useCallback((providerName: string) => {
    setExpandedProvider(expandedProvider === providerName ? null : providerName);
  }, [expandedProvider]);

  const resetWebSearchDraft = useCallback(() => {
    if (!settings) return;
    setWebSearchForm({
      provider: settings.web_search.provider,
      apiKey: "",
      baseUrl: settings.web_search.base_url ?? "",
      maxResults: settings.web_search.max_results,
      timeout: settings.web_search.timeout,
      useJinaReader: settings.web.fetch.use_jina_reader,
    });
    setWebSearchKeyVisible(false);
    setWebSearchKeyEditing(false);
  }, [settings]);

  const handleWebSearchProviderChange = useCallback((provider: string) => {
    if (!settings) return;
    setWebSearchForm((prev) => ({
      provider,
      apiKey: "",
      baseUrl: provider === settings.web_search.provider ? settings.web_search.base_url ?? "" : "",
      maxResults: prev.maxResults ?? settings.web_search.max_results,
      timeout: prev.timeout ?? settings.web_search.timeout,
      useJinaReader: prev.useJinaReader ?? settings.web.fetch.use_jina_reader,
    }));
    setWebSearchKeyVisible(false);
    setWebSearchKeyEditing(false);
  }, [settings]);

  const handleAutomationAction = async (
    action: AutomationAction,
    job: SessionAutomationJob,
  ) => {
    const key = `${action}:${job.id}`;
    setAutomationAction(key);
    setAutomationsError(null);
    try {
      const payload = await runAutomationAction(token, action, job.id);
      setAutomations(payload);
      if (action === "delete") setAutomationPendingDelete(null);
      if (action === "run") {
        window.setTimeout(() => void refreshAutomations(false), 1200);
        window.setTimeout(() => void refreshAutomations(false), 4000);
      }
    } catch (err) {
      setAutomationsError((err as Error).message);
    } finally {
      setAutomationAction(null);
    }
  };

  const handleAutomationEdit = async (
    job: SessionAutomationJob,
    values: AutomationUpdatePayload,
  ) => {
    const key = `update:${job.id}`;
    setAutomationAction(key);
    setAutomationsError(null);
    try {
      const payload = await updateAutomation(token, job.id, values);
      setAutomations(payload);
      setAutomationPendingEdit(null);
    } catch (err) {
      setAutomationsError((err as Error).message);
    } finally {
      setAutomationAction(null);
    }
  };

  /** 「模型」tab：二级切换条 + 各子区面板（LLM / 文生图 / 文生视频占位 / 视图理解 / ASR）。 */
  const renderModelsTab = (sub: SettingsSectionKey, settings: SettingsPayload) => (
    <div className="space-y-8">
      <SettingsSubTabs tabs={MODEL_SUB_TABS} active={sub} onSelect={selectSection} />
      {sub === "models" ? (
        <div className="space-y-8">
          <ModelsSettings
            token={token}
            form={form}
            setForm={setForm}
            settings={settings}
            dirty={modelDirty}
            saving={saving}
            showBrandLogos={localPrefs.brandLogos}
            onSave={saveModelSettings}
            onCreateConfiguration={openModelConfigurationDialog}
          />
        </div>
      ) : null}
      {sub === "providers" ? (
        <ProvidersSettings
          token={token}
          settings={settings}
          expandedProvider={expandedProvider}
          query={providerQuery}
          showBrandLogos={localPrefs.brandLogos}
          onQueryChange={setProviderQuery}
          onToggleProvider={handleToggleProvider}
          onProviderSaved={applyPayload}
        />
      ) : null}
      {sub === "image" ? (
        <ImageGenerationSettings
          token={token}
          settings={settings}
          form={imageGenerationForm}
          dirty={imageGenerationDirty}
          saving={imageGenerationSaving}
          onChangeForm={setImageGenerationForm}
          onSave={saveImageGenerationSettings}
          onOpenProviders={() => selectSection("providers")}
          showBrandLogos={localPrefs.brandLogos}
          onRestart={restartViaSettingsSurface}
          isRestarting={isRestarting || hostEngineApplying}
          requiresRestartPending={pendingRestartSections.image}
        />
      ) : null}
      {sub === "video" ? (
        <VideoGenerationSettings
          settings={settings}
          form={videoGenerationForm}
          dirty={videoGenerationDirty}
          saving={videoGenerationSaving}
          onChangeForm={setVideoGenerationForm}
          onSave={saveVideoGenerationSettings}
          onOpenProviders={() => selectSection("providers")}
          onRestart={restartViaSettingsSurface}
          isRestarting={isRestarting || hostEngineApplying}
          requiresRestartPending={pendingRestartSections.video}
        />
      ) : null}
      {sub === "vision" ? (
        <VisionSettings
          settings={settings}
          form={screenshotForm}
          dirty={screenshotDirty}
          saving={screenshotSaving}
          onChangeForm={setScreenshotForm}
          onSave={saveScreenshotSettings}
          onOpenModels={() => selectSection("models")}
          onRestart={restartViaSettingsSurface}
          isRestarting={isRestarting || hostEngineApplying}
          requiresRestartPending={pendingRestartSections.vision}
        />
      ) : null}
      {sub === "voice" ? (
        <TranscriptionSettings
          token={token}
          settings={settings}
          form={transcriptionForm}
          dirty={transcriptionDirty}
          saving={transcriptionSaving}
          onChangeForm={setTranscriptionForm}
          onSave={saveTranscriptionSettings}
          onOpenProviders={() => selectSection("providers")}
          showBrandLogos={localPrefs.brandLogos}
          onRestart={restartViaSettingsSurface}
          isRestarting={isRestarting || hostEngineApplying}
          requiresRestartPending={pendingRestartSections.browser}
        />
      ) : null}
      {sub === "tts" ? (
        <TtsSettings
          token={token}
          settings={settings}
          form={ttsForm}
          dirty={ttsDirty}
          saving={ttsSaving}
          onChangeForm={setTtsForm}
          onSave={saveTtsSettings}
          onOpenProviders={() => selectSection("providers")}
          showBrandLogos={localPrefs.brandLogos}
        />
      ) : null}
    </div>
  );

  /** 「系统」tab：二级切换条 + 各子区面板（运行 / 系统IO / 安全）。 */
  const renderSystemTab = (sub: SettingsSectionKey, settings: SettingsPayload) => (
    <div className="space-y-8">
      <SettingsSubTabs tabs={SYSTEM_SUB_TABS} active={sub} onSelect={selectSection} />
      {sub === "runtime" ? (
        <RuntimeSettings
          form={form}
          setForm={setForm}
          settings={settings}
          dirty={runtimeDirty}
          saving={saving}
          onSave={saveRuntimeSettings}
          onRestart={restartViaSettingsSurface}
          isRestarting={isRestarting || hostEngineApplying}
          requiresRestartPending={pendingRestartSections.runtime}
        />
      ) : null}
      {sub === "systemIo" ? (
        <SystemIoSettings
          settings={settings}
          form={systemIoForm}
          dirty={systemIoDirty}
          saving={systemIoSaving}
          onChangeForm={setSystemIoForm}
          onSave={saveSystemIoSettings}
          onRestart={restartViaSettingsSurface}
          isRestarting={isRestarting || hostEngineApplying}
          requiresRestartPending={pendingRestartSections.systemIo}
        />
      ) : null}
      {sub === "advanced" ? (
        <AdvancedSettings
          form={networkSafetyForm}
          dirty={networkSafetyDirty}
          saving={networkSafetySaving}
          isNativeHostSurface={(settings.surface ?? settings.runtime_surface) === "native"}
          onChangeForm={setNetworkSafetyForm}
          onSave={saveNetworkSafetySettings}
          onRestart={restartViaSettingsSurface}
          isRestarting={isRestarting || hostEngineApplying}
          requiresRestartPending={pendingRestartSections.runtime}
        />
      ) : null}
    </div>
  );

  const renderSection = () => {
    if (!settings) return null;
    switch (activeSection) {
      case "overview":
        return (
          <OverviewSettings
            settings={settings}
            requiresRestart={hasPendingRestart}
            showBrandLogos={localPrefs.brandLogos}
            onSelectSection={selectSection}
          />
        );
      case "appearance":
        return (
          <AppearanceSettings
            theme={theme}
            onToggleTheme={onToggleTheme}
            localPrefs={localPrefs}
            onChangeLocalPrefs={setLocalPrefs}
          />
        );
      case "models":
      case "providers":
      case "image":
      case "video":
      case "vision":
      case "voice":
      case "tts":
        return renderModelsTab(activeSection, settings);
      case "browser":
        return (
          <WebSettings
            settings={settings}
            form={webSearchForm}
            keyVisible={webSearchKeyVisible}
            keyEditing={webSearchKeyEditing}
            saving={webSearchSaving}
            onChangeForm={setWebSearchForm}
            onChangeProvider={handleWebSearchProviderChange}
            onToggleKey={() => setWebSearchKeyVisible((visible) => !visible)}
            onToggleKeyEditing={() => {
              setWebSearchKeyEditing((editing) => !editing);
              setWebSearchKeyVisible(false);
              setWebSearchForm((prev) => ({ ...prev, apiKey: "" }));
            }}
            onReset={resetWebSearchDraft}
            onSave={saveWebSearch}
            showBrandLogos={localPrefs.brandLogos}
            onRestart={restartViaSettingsSurface}
            isRestarting={isRestarting || hostEngineApplying}
            requiresRestartPending={pendingRestartSections.browser}
          />
        );
      case "automations":
        return (
          <AutomationsSettings
            payload={automations}
            loading={automationsLoading}
            query={automationsQuery}
            filter={automationsFilter}
            sort={automationsSort}
            actionKey={automationAction}
            error={automationsError}
            onQueryChange={setAutomationsQuery}
            onFilterChange={setAutomationsFilter}
            onSortChange={setAutomationsSort}
            onAction={handleAutomationAction}
            onRequestEdit={setAutomationPendingEdit}
            onRequestDelete={setAutomationPendingDelete}
          />
        );
      case "skills":
        return <SkillsCatalogSettings skills={skills} onDeleted={onSkillsDeleted} />;
      case "capabilities":
        return <CapabilitiesView onSkillsDeleted={onSkillsDeleted} />;
      case "channels":
        return (
          <ChannelsSettings
            token={token}
            onRestart={restartViaSettingsSurface}
            isRestarting={isRestarting || hostEngineApplying}
          />
        );
      case "runtime":
      case "systemIo":
      case "advanced":
        return renderSystemTab(activeSection, settings);
      default:
        return null;
    }
  };

  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 flex-col overflow-hidden md:flex-row",
        "cyber-shell-bg",
      )}
    >
      {showSidebar ? (
        <SettingsSidebar
          activeSection={activeSection}
          onSelectSection={selectSection}
          onBackToChat={onBackToChat}
          onLogout={onLogout}
          hostChromeInset={hostChromeInset}
          hideLogout={hideLogout}
        />
      ) : null}

      <NewModelConfigurationDialog
        open={modelConfigurationOpen}
        draft={modelConfigurationForm}
        providers={configuredModelProviderOptions}
        saving={modelConfigurationSaving}
        showProviderLogos={localPrefs.brandLogos}
        onOpenChange={setModelConfigurationOpen}
        onChangeDraft={setModelConfigurationForm}
        onSave={handleCreateModelConfiguration}
      />

      <AutomationDeleteDialog
        job={automationPendingDelete}
        deleting={automationAction === `delete:${automationPendingDelete?.id ?? ""}`}
        onOpenChange={(open) => {
          if (!open) setAutomationPendingDelete(null);
        }}
        onConfirm={(job) => handleAutomationAction("delete", job)}
      />

      <AutomationEditDialog
        job={automationPendingEdit}
        saving={automationAction === `update:${automationPendingEdit?.id ?? ""}`}
        onOpenChange={(open) => {
          if (!open) setAutomationPendingEdit(null);
        }}
        onSave={handleAutomationEdit}
      />

      <main className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
        <div
          className={cn(
            "mx-auto w-full max-w-[920px] px-4 py-6 sm:px-8 sm:py-8 lg:py-12",
            hostChromeInset && "pt-11 sm:pt-11 lg:pt-11",
          )}
        >
          <div className="mb-7">
            {!showSidebar ? (
              <button
                type="button"
                onClick={onBackToChat}
                className="mb-4 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground lg:hidden"
              >
                <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
                {t("settings.backToChat")}
              </button>
            ) : null}
            {showSidebar ? (
              <p className="mb-2 text-[12px] font-normal text-muted-foreground">
                {t("settings.sidebar.title")}
              </p>
            ) : null}
            <h1 className="text-[24px] font-normal leading-tight tracking-normal text-foreground sm:text-[28px]">
              {text(
                `settings.nav.${topLevelSection(activeSection)}`,
                titleForSection(topLevelSection(activeSection)),
              )}
            </h1>
          </div>

          {loading ? (
            <div className="cyber-glass-panel relative overflow-hidden flex h-48 items-center justify-center rounded-[24px] text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {t("settings.status.loading")}
            </div>
          ) : error && !settings ? (
            <SettingsGroup>
              <SettingsRow title={t("settings.status.loadError")}>
                <span className="max-w-[520px] text-sm text-muted-foreground">{error}</span>
              </SettingsRow>
            </SettingsGroup>
          ) : settings ? (
            <div className="space-y-5">
              {error ? (
                <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
                  {error}
                </div>
              ) : null}
              {renderSection()}
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}

/** 侧边栏顶层标签：概览 / 外观 / 模型 / 网页 / 系统。模型与系统各自带二级子区。 */
const SETTINGS_NAV_ITEMS: Array<{ key: SettingsSectionKey; icon: LucideIcon; fallback: string }> = [
  { key: "overview", icon: Activity, fallback: "Overview" },
  { key: "appearance", icon: Palette, fallback: "Appearance" },
  { key: "models", icon: SlidersHorizontal, fallback: "Models" },
  { key: "browser", icon: Globe2, fallback: "Web" },
  { key: "channels", icon: MessageSquareText, fallback: "Channels" },
  { key: "runtime", icon: Server, fallback: "System" },
];

/** 「模型」父标签家族的 section（含未上线的文生视频占位）。 */
const MODEL_TAB_KEYS: SettingsSectionKey[] = ["models", "providers", "image", "video", "vision", "voice", "tts"];
/** 「系统」父标签家族的 section。 */
const SYSTEM_TAB_KEYS: SettingsSectionKey[] = ["runtime", "systemIo", "advanced"];

/** 子区 section → 顶层标签键（侧边栏高亮与页头标题归一化用）。 */
function topLevelSection(section: SettingsSectionKey): SettingsSectionKey {
  if (MODEL_TAB_KEYS.includes(section)) return "models";
  if (SYSTEM_TAB_KEYS.includes(section)) return "runtime";
  return section;
}

/** 模型 tab 二级切换条（子区键即 section key，点选即 selectSection）。 */
const MODEL_SUB_TABS: Array<{ key: SettingsSectionKey; labelKey: string; fallback: string }> = [
  { key: "providers", labelKey: "settings.subtabs.model.providers", fallback: "模型厂商" },
  { key: "models", labelKey: "settings.subtabs.model.llm", fallback: "LLM" },
  { key: "image", labelKey: "settings.subtabs.model.image", fallback: "文生图" },
  { key: "video", labelKey: "settings.subtabs.model.video", fallback: "文生视频" },
  { key: "vision", labelKey: "settings.subtabs.model.vision", fallback: "视图理解" },
  { key: "voice", labelKey: "settings.subtabs.model.asr", fallback: "ASR" },
  { key: "tts", labelKey: "settings.subtabs.model.tts", fallback: "TTS" },
];

/** 系统 tab 二级切换条。 */
const SYSTEM_SUB_TABS: Array<{ key: SettingsSectionKey; labelKey: string; fallback: string }> = [
  { key: "runtime", labelKey: "settings.subtabs.system.runtime", fallback: "运行" },
  { key: "systemIo", labelKey: "settings.subtabs.system.systemIo", fallback: "系统 IO" },
  { key: "advanced", labelKey: "settings.subtabs.system.advanced", fallback: "安全" },
];

function visibleWebuiDefaultAccessMode(mode: string | null | undefined): WebuiDefaultAccessMode {
  return mode === "full" ? "full" : "default";
}

function titleForSection(section: SettingsSectionKey): string {
  return SETTINGS_NAV_ITEMS.find((item) => item.key === section)?.fallback ?? "Settings";
}

function SettingsSidebar({
  activeSection,
  onSelectSection,
  onBackToChat,
  onLogout,
  hostChromeInset,
  hideLogout,
}: {
  activeSection: SettingsSectionKey;
  onSelectSection: (section: SettingsSectionKey) => void;
  onBackToChat: () => void;
  onLogout?: () => void;
  hostChromeInset?: boolean;
  hideLogout?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <aside
      className={cn(
        "flex w-full shrink-0 flex-col border-b border-[hsl(var(--cyber-panel-border)/0.28)] bg-card/62 px-3 pb-2 shadow-[inset_0_-1px_0_rgba(255,255,255,0.55)] backdrop-blur-xl dark:bg-card/45 dark:shadow-none md:w-[17rem] md:border-b-0 md:border-r md:px-3 md:pb-4 md:shadow-[inset_-1px_0_0_rgba(255,255,255,0.55)]",
        hostChromeInset ? "pt-11 md:pt-11" : "pt-4 md:pt-4",
      )}
    >
      <button
        type="button"
        onClick={onBackToChat}
        className="mb-2 inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground md:mb-3"
      >
        <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
        {t("settings.backToChat")}
      </button>
      <div className="mb-3 px-1 md:mb-4 md:px-2">
        <h2 className="text-[18px] font-normal tracking-normal text-foreground">
          {t("settings.sidebar.title")}
        </h2>
      </div>

      <nav
        aria-label={t("settings.sidebar.ariaLabel")}
        className="-mx-1 flex gap-2 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden md:mx-0 md:block md:space-y-1 md:overflow-visible md:px-0 md:pb-0"
      >
        {SETTINGS_NAV_ITEMS.map(({ key, icon: Icon, fallback }) => {
          const active = topLevelSection(key) === topLevelSection(activeSection);
          return (
            <button
              key={key}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() => onSelectSection(key)}
              className={cn(
                "flex h-9 w-auto shrink-0 items-center gap-2 rounded-full px-3 text-left text-[13px] font-medium transition-colors md:w-full md:rounded-[10px] md:px-2.5",
                active
                  ? "bg-muted/90 text-foreground shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)]"
                  : "text-muted-foreground/78 hover:bg-muted/45 hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" strokeWidth={2} aria-hidden />
              <span className="truncate">{t(`settings.nav.${key}`, { defaultValue: fallback })}</span>
            </button>
          );
        })}
      </nav>

      <div className="hidden md:mt-auto md:block md:pt-4">
        {onLogout && !hideLogout ? (
          <Button
            type="button"
            variant="ghost"
            onClick={onLogout}
            className="h-9 w-full justify-start gap-2 rounded-[10px] px-2.5 text-[13px] font-medium text-muted-foreground hover:bg-destructive/8 hover:text-destructive"
          >
            <LogOut className="h-4 w-4" aria-hidden />
            {t("app.account.logout")}
          </Button>
        ) : null}
      </div>
    </aside>
  );
}

/** 模型/系统 tab 内的二级切换条。子区键即 section key，onSelect 直接复用 selectSection。 */
function SettingsSubTabs({
  tabs,
  active,
  onSelect,
}: {
  tabs: Array<{ key: SettingsSectionKey; labelKey: string; fallback: string }>;
  active: SettingsSectionKey;
  onSelect: (section: SettingsSectionKey) => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid="settings-subtabs"
      className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:mx-0 sm:flex-wrap sm:px-0"
    >
      {tabs.map((tab) => {
        const selected = tab.key === active;
        return (
          <button
            key={tab.key}
            type="button"
            aria-current={selected ? "true" : undefined}
            onClick={() => onSelect(tab.key)}
            className={cn(
              "flex h-9 shrink-0 items-center gap-1.5 rounded-[10px] px-3 text-[13px] font-medium transition-colors",
              selected
                ? "bg-muted/90 text-foreground shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)]"
                : "text-muted-foreground/78 hover:bg-muted/45 hover:text-foreground",
            )}
          >
            {t(tab.labelKey, { defaultValue: tab.fallback })}
          </button>
        );
      })}
    </div>
  );
}

function OverviewSettings({
  settings,
  requiresRestart,
  onSelectSection,
  showBrandLogos,
}: {
  settings: SettingsPayload;
  requiresRestart: boolean;
  onSelectSection: (section: SettingsSectionKey) => void;
  showBrandLogos: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const activePreset = settings.agent.model_preset || "default";
  const activeProvider = settings.agent.resolved_provider ?? settings.agent.provider;
  const activeProviderConfigured = settingsProviderConfigured(settings, activeProvider);
  const activeProviderLabel = providerDisplayLabel(settings.providers, activeProvider);
  const activeModelValue = activeProviderConfigured
    ? settings.agent.model
    : tx("settings.values.notConfigured", "Not configured");
  const activeModelCaption = activeProviderConfigured
    ? `${activeProvider} · ${activePreset}`
    : activeProviderLabel || settings.agent.model
      ? [activeProviderLabel, settings.agent.model].filter(Boolean).join(" · ")
      : tx("settings.byok.noConfiguredProviders", "No configured providers");
  const webStatus = settings.web.enable
    ? tx("settings.values.enabled", "Enabled")
    : tx("settings.values.disabled", "Disabled");
  const webSearchProvider =
    settings.web_search.providers.find((provider) => provider.name === settings.web_search.provider) ??
    settings.web_search.providers[0];
  const webSearchProviderLabel = providerDisplayLabel(
    settings.web_search.providers,
    settings.web_search.provider,
  );
  const webSearchCredentialStatus =
    webSearchProvider?.credential === "none"
      ? tx("settings.byok.webSearch.noCredentialRequired", "No key required")
      : webSearchProvider?.credential === "base_url"
        ? settings.web_search.base_url
          ? tx("settings.values.configured", "Configured")
          : tx("settings.values.notConfigured", "Not configured")
        : settings.web_search.api_key_hint
          ? tx("settings.values.configured", "Configured")
          : tx("settings.values.notConfigured", "Not configured");
  const webCaption = `${webSearchProviderLabel} · ${webSearchCredentialStatus}`;
  const imageStatus = settings.image_generation.enabled
    ? tx("settings.values.enabled", "Enabled")
    : tx("settings.values.disabled", "Disabled");
  const imageCaption = `${providerDisplayLabel(settings.providers, settings.image_generation.provider)} · ${
    settings.image_generation.provider_configured
      ? tx("settings.values.configured", "Configured")
      : tx("settings.values.notConfigured", "Not configured")
  }`;
  const transcription = settings.transcription ?? DEFAULT_TRANSCRIPTION_SETTINGS;
  const voiceStatus = transcription.enabled
    ? tx("settings.values.enabled", "Enabled")
    : tx("settings.values.disabled", "Disabled");
  const voiceCaption = `${providerDisplayLabel(settings.providers, transcription.provider)} · ${
    transcription.provider_configured
      ? tx("settings.values.configured", "Configured")
      : tx("settings.values.notConfigured", "Not configured")
  }`;
  const visionStatus = settings.screenshot.enabled
    ? tx("settings.values.enabled", "Enabled")
    : tx("settings.values.disabled", "Disabled");
  const visionProviderRow =
    settings.providers.find(
      (provider) => provider.name === settings.screenshot.vision_model,
    ) ?? null;
  const visionPreset =
    settings.model_presets.find((preset) => preset.name === settings.screenshot.vision_model) ?? null;
  const visionResolvedModel =
    settings.screenshot.resolved_model
    ?? (visionPreset?.model ?? settings.screenshot.vision_model ?? null);
  const visionCaption = `${visionProviderRow?.label ?? settings.screenshot.vision_model ?? tx("settings.vision.selectProvider", "Select provider")} · ${visionResolvedModel ?? "—"} · ${
    settings.screenshot.vision_model_configured
      ? tx("settings.values.configured", "Configured")
      : tx("settings.values.notConfigured", "Not configured")
  }`;
  const systemIoStatus = settings.system_io.enabled
    ? tx("settings.values.enabled", "Enabled")
    : tx("settings.values.disabled", "Disabled");
  const systemIoCaption = settings.system_io.enabled
    ? settings.system_io.allow_actions.length === 0
      ? tx("settings.values.systemIoAllActions", "All permitted")
      : `${settings.system_io.allow_actions.length} / ${settings.system_io.available_actions.length} ${tx("settings.values.systemIoRestricted", "Restricted").toLowerCase()}`
    : tx("settings.values.disabled", "Disabled");
  const isNativeHost = (settings.surface ?? settings.runtime_surface) === "native";
  const workspaceCaption = shortWorkspacePath(settings.runtime.workspace_path);
  const runtimeTitle = isNativeHost
    ? tx("settings.rows.engine", "Engine")
    : tx("settings.rows.gateway", "Gateway");
  const runtimeValue = isNativeHost
    ? tx("settings.values.privateEngine", "Private engine")
    : `${settings.runtime.gateway_host}:${settings.runtime.gateway_port}`;
  const runtimeCaption = isNativeHost
    ? tx("settings.values.unixSocket", "Unix socket")
    : requiresRestart
      ? tx("settings.values.restartPending", "Restart pending")
      : tx("settings.values.ready", "Ready");
  return (
    <div className="space-y-7">
      <section>
        <TokenUsageHeatmap usage={settings.usage} timeZone={settings.agent.timezone} />
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.ai", "AI")}</SettingsSectionTitle>
        <SettingsGroup>
          <OverviewListRow
            icon={Bot}
            valueLogoProvider={activeProvider}
            title={tx("settings.overview.model", "Current model")}
            value={activeModelValue}
            caption={activeModelCaption}
            showBrandLogos={showBrandLogos}
            onClick={() => onSelectSection("models")}
          />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.capabilities", "Capabilities")}</SettingsSectionTitle>
        <SettingsGroup>
          <OverviewListRow
            icon={Globe2}
            valueLogoProvider={settings.web_search.provider}
            title={tx("settings.overview.webSearch", "Web search")}
            value={webStatus}
            caption={webCaption}
            showBrandLogos={showBrandLogos}
            onClick={() => onSelectSection("browser")}
          />
          <OverviewListRow
            icon={ImageIcon}
            valueLogoProvider={settings.image_generation.provider}
            title={tx("settings.overview.imageGeneration", "Image generation")}
            value={imageStatus}
            caption={imageCaption}
            showBrandLogos={showBrandLogos}
            onClick={() => onSelectSection("image")}
          />
          <OverviewListRow
            icon={Eye}
            title={tx("settings.overview.vision", "Vision")}
            value={visionStatus}
            caption={visionCaption}
            showBrandLogos={showBrandLogos}
            onClick={() => onSelectSection("vision")}
          />
          <OverviewListRow
            icon={Mic}
            valueLogoProvider={transcription.provider}
            title={tx("settings.overview.voiceInput", "Voice input")}
            value={voiceStatus}
            caption={voiceCaption}
            showBrandLogos={showBrandLogos}
            onClick={() => onSelectSection("voice")}
          />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.system", "System")}</SettingsSectionTitle>
        <SettingsGroup>
          <OverviewListRow
            icon={Server}
            title={runtimeTitle}
            value={runtimeValue}
            caption={runtimeCaption}
            onClick={() => onSelectSection("runtime")}
          />
          <OverviewListRow
            icon={HardDrive}
            title={tx("settings.overview.workspace", "Workspace")}
            value={tx("settings.values.defaultWorkspace", "Default workspace")}
            caption={workspaceCaption}
            onClick={() => onSelectSection("runtime")}
          />
          <OverviewListRow
            icon={Cpu}
            title={tx("settings.overview.systemIo", "System IO")}
            value={systemIoStatus}
            caption={systemIoCaption}
            onClick={() => onSelectSection("systemIo")}
          />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.about", "About")}</SettingsSectionTitle>
        <SettingsGroup>
          <VersionCheckRow currentVersion={settings.version?.current} />
        </SettingsGroup>
      </section>
    </div>
  );
}

function VersionCheckRow({ currentVersion }: { currentVersion?: string }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { token } = useClient();
  const [checking, setChecking] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [result, setResult] = useState<
    | { type: "up-to-date" }
    | { type: "update"; latestVersion: string; pypiUrl?: string }
    | { type: "updated"; newVersion?: string }
    | { type: "error"; message: string }
    | null
  >(null);

  const handleCheck = async () => {
    setChecking(true);
    setResult(null);
    try {
      const res = await checkVersion(token);
      if (res.updateAvailable) {
        setResult({
          type: "update",
          latestVersion: res.updateAvailable.latestVersion,
          pypiUrl: res.updateAvailable.pypiUrl,
        });
      } else {
        setResult({ type: "up-to-date" });
      }
    } catch (err) {
      setResult({ type: "error", message: (err as Error).message });
    } finally {
      setChecking(false);
    }
  };

  const handleSelfUpdate = async () => {
    setUpdating(true);
    try {
      const res = await selfUpdate(token);
      if (res.selfUpdate?.success) {
        setResult({ type: "updated", newVersion: res.selfUpdate.newVersion });
      } else {
        setResult({ type: "error", message: res.selfUpdate?.output || "更新失败" });
      }
    } catch (err) {
      setResult({ type: "error", message: (err as Error).message });
    } finally {
      setUpdating(false);
    }
  };

  return (
    <div className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0">
        <div className="text-[14px] font-medium leading-5 text-foreground">
          {tx("settings.about.version", "Version")}
        </div>
        <div className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
          {currentVersion ? `v${currentVersion}` : "biscuitbot"}
        </div>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => void handleCheck()}
          disabled={checking || updating}
          className="rounded-full"
        >
          {checking ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <ArrowUpCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          )}
          {checking
            ? tx("settings.about.checking", "Checking...")
            : tx("settings.about.checkForUpdates", "检查更新")}
        </Button>
        {result?.type === "up-to-date" ? (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-emerald-600 dark:text-emerald-300">
            <Check className="h-3 w-3" aria-hidden />
            {tx("settings.about.upToDate", "已是最新版本")}
          </span>
        ) : null}
        {result?.type === "update" ? (
          <div className="flex flex-col items-end gap-2">
            <span className="inline-flex items-center gap-1.5 text-[12px] text-blue-600 dark:text-blue-300">
              <ArrowUpCircle className="h-3 w-3" aria-hidden />
              {t("settings.about.updateAvailable", {
                defaultValue: "有可用更新 v{{version}}",
                version: result.latestVersion,
              })}
            </span>
            <Button
              size="sm"
              variant="default"
              onClick={() => void handleSelfUpdate()}
              disabled={updating}
              className="rounded-full"
            >
              {updating ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <ArrowUpCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              )}
              {updating
                ? tx("settings.about.updating", "正在更新...")
                : tx("settings.about.updateNow", "立即更新")}
            </Button>
          </div>
        ) : null}
        {result?.type === "updated" ? (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-emerald-600 dark:text-emerald-300">
            <Check className="h-3 w-3" aria-hidden />
            {tx("settings.about.updateSuccess", "更新成功，请重启应用")}
            {result.newVersion ? ` (v${result.newVersion})` : ""}
          </span>
        ) : null}
        {result?.type === "error" ? (
          <span className="text-[12px] text-destructive">{result.message}</span>
        ) : null}
      </div>
    </div>
  );
}

function AppearanceSettings({
  theme,
  onToggleTheme,
  localPrefs,
  onChangeLocalPrefs,
}: {
  theme: "light" | "dark";
  onToggleTheme: () => void;
  localPrefs: LocalPreferences;
  onChangeLocalPrefs: Dispatch<SetStateAction<LocalPreferences>>;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{t("settings.sections.interface")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.rows.theme")}
            description={t("settings.help.theme")}
          >
            <button
              type="button"
              onClick={onToggleTheme}
              className="inline-flex h-8 items-center rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground"
            >
              <span
                className={cn(
                  "rounded-full px-3 py-1 transition-colors",
                  theme === "light" && "bg-background text-foreground shadow-sm",
                )}
              >
                {t("settings.values.light")}
              </span>
              <span
                className={cn(
                  "rounded-full px-3 py-1 transition-colors",
                  theme === "dark" && "bg-background text-foreground shadow-sm",
                )}
              >
                {t("settings.values.dark")}
              </span>
            </button>
          </SettingsRow>

          <SettingsRow
            title={t("settings.rows.language")}
            description={t("settings.help.language")}
          >
            <LanguageSwitcher />
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.localPreferences", "Local preferences")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.density", "Density")}
            description={tx("settings.help.density", "Stored only in this browser.")}
          >
            <SegmentedControl
              value={localPrefs.density}
              options={[
                { value: "comfortable", label: tx("settings.values.comfortable", "Comfortable") },
                { value: "compact", label: tx("settings.values.compact", "Compact") },
              ]}
              onChange={(density) =>
                onChangeLocalPrefs((prev) => ({ ...prev, density: density as LocalDensity }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.activityMode", "Activity detail")}
            description={tx("settings.help.activityMode", "Choose how much agent activity chrome to show by default.")}
          >
            <SegmentedControl
              value={localPrefs.activityMode}
              options={[
                { value: "auto", label: tx("settings.values.auto", "Auto") },
                { value: "expanded", label: tx("settings.values.expanded", "Expanded") },
              ]}
              onChange={(activityMode) =>
                onChangeLocalPrefs((prev) => ({ ...prev, activityMode: activityMode as LocalActivityMode }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.codeWrap", "Code wrapping")}
            description={tx("settings.help.codeWrap", "Keep long code lines readable on smaller screens.")}
          >
            <ToggleButton
              checked={localPrefs.codeWrap}
              onChange={(codeWrap) => onChangeLocalPrefs((prev) => ({ ...prev, codeWrap }))}
              ariaLabel={tx("settings.rows.codeWrap", "Code wrapping")}
              label={localPrefs.codeWrap ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.brandLogos", "Brand logos")}
            description={tx("settings.help.brandLogos", "Show third-party provider and CLI logos in Settings.")}
          >
            <ToggleButton
              checked={localPrefs.brandLogos}
              onChange={(brandLogos) => onChangeLocalPrefs((prev) => ({ ...prev, brandLogos }))}
              ariaLabel={tx("settings.rows.brandLogos", "Brand logos")}
              label={localPrefs.brandLogos ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
        </SettingsGroup>
      </section>
    </div>
  );
}

function NewModelConfigurationDialog({
  open,
  draft,
  providers,
  saving,
  showProviderLogos,
  onOpenChange,
  onChangeDraft,
  onSave,
}: {
  open: boolean;
  draft: ModelConfigurationDraft;
  providers: Array<{ name: string; label: string }>;
  saving: boolean;
  showProviderLogos: boolean;
  onOpenChange: (open: boolean) => void;
  onChangeDraft: Dispatch<SetStateAction<ModelConfigurationDraft>>;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const canSave = Boolean(draft.label.trim() && draft.provider.trim() && draft.model.trim());

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[520px] rounded-[28px] border-border/55 bg-card/95 p-0 shadow-[0_28px_90px_rgba(15,23,42,0.20)] backdrop-blur-xl dark:border-white/10">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSave();
          }}
        >
          <DialogHeader className="border-b border-border/45 px-5 py-4 text-left">
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">
              {tx("settings.models.newConfiguration", "New model configuration")}
            </DialogTitle>
            <DialogDescription className="text-[12.5px] leading-5">
              {tx("settings.models.newConfigurationHelp", "Save a provider and model as a one-click option.")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-5 py-5">
            <label className="block">
              <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                {tx("settings.models.configurationName", "Configuration name")}
              </span>
              <Input
                autoFocus
                value={draft.label}
                placeholder={tx("settings.models.configurationNamePlaceholder", "Fast writing")}
                onChange={(event) =>
                  onChangeDraft((prev) => ({ ...prev, label: event.target.value }))
                }
                className="h-10 rounded-full px-4 text-[14px]"
              />
            </label>

            <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
              <label className="block">
                <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                  {tx("settings.rows.model", "Model")}
                </span>
                <Input
                  value={draft.model}
                  placeholder="openai/gpt-4.1"
                  onChange={(event) =>
                    onChangeDraft((prev) => ({ ...prev, model: event.target.value }))
                  }
                  className="h-10 rounded-full px-4 text-[14px]"
                />
              </label>
              <div className="block">
                <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                  {tx("settings.rows.provider", "Provider")}
                </span>
                <ProviderPicker
                  providers={providers}
                  value={draft.provider}
                  emptyLabel={tx("settings.byok.noConfiguredProviders", "No configured providers")}
                  showProviderLogos={showProviderLogos}
                  onChange={(provider) =>
                    onChangeDraft((prev) => ({ ...prev, provider }))
                  }
                />
              </div>
            </div>
          </div>

          <DialogFooter className="border-t border-border/45 px-5 py-4 sm:space-x-2">
            <Button
              type="button"
              variant="ghost"
              className="rounded-full"
              disabled={saving}
              onClick={() => onOpenChange(false)}
            >
              {tx("settings.actions.cancel", "Cancel")}
            </Button>
            <Button
              type="submit"
              variant="outline"
              className="rounded-full"
              disabled={!canSave || saving || providers.length === 0}
            >
              {saving ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {saving ? tx("settings.actions.saving", "Saving...") : tx("settings.actions.save", "Save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ModelsSettings({
  token,
  form,
  setForm,
  settings,
  dirty,
  saving,
  showBrandLogos,
  onSave,
  onCreateConfiguration,
}: {
  token: string;
  form: AgentSettingsDraft;
  setForm: Dispatch<SetStateAction<AgentSettingsDraft>>;
  settings: SettingsPayload;
  dirty: boolean;
  saving: boolean;
  showBrandLogos: boolean;
  onSave: () => void;
  onCreateConfiguration: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const configuredProviders = settings.providers.filter((provider) => provider.configured);
  const showAutoProvider = defaultPreset(settings)?.provider === "auto" || form.provider === "auto";
  const selectableProviders = uniqueProviders(configuredProviders);
  const providerOptions = showAutoProvider
    ? [{ name: "auto", label: tx("settings.values.auto", "Auto") }, ...selectableProviders]
    : selectableProviders;
  const providerValue = providerOptions.some((provider) => provider.name === form.provider)
    ? form.provider
    : "";
  const selectedPreset =
    settings.model_presets.find((preset) => preset.name === form.modelPreset) ?? null;
  const selectedProviderConfigured = settingsProviderConfigured(settings, form.provider);
  const modelFieldsMissing =
    !form.model.trim() ||
    !form.provider.trim() ||
    Boolean(selectedPreset && !selectedPreset.is_default && !form.presetLabel.trim());
  return (
    <div className="space-y-7">
      <section>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.currentModel", "Current configuration")}
            description={tx("settings.help.currentModel", "Used for new replies.")}
          >
            <ModelPresetPicker
              presets={settings.model_presets}
              value={form.modelPreset}
              settings={settings}
              draftModel={form.model}
              draftProvider={form.provider}
              providerConfigured={selectedProviderConfigured}
              showProviderLogos={showBrandLogos}
              onChange={(modelPreset) => {
                const nextPreset = settings.model_presets.find((preset) => preset.name === modelPreset);
                setForm((prev) => ({
                  ...prev,
                  modelPreset,
                  model: nextPreset?.model ?? prev.model,
                  provider: nextPreset?.is_default
                    ? editableDefaultProvider(settings)
                    : nextPreset?.provider ?? prev.provider,
                  presetLabel: nextPreset?.label ?? modelPreset,
                  contextWindowTokens: normalizeContextWindowTokens(
                    nextPreset?.context_window_tokens ?? prev.contextWindowTokens,
                  ),
                }));
              }}
              onCreateConfiguration={onCreateConfiguration}
            />
          </SettingsRow>
          {selectedPreset && !selectedPreset.is_default ? (
            <SettingsRow
              title={tx("settings.models.configurationName", "Configuration name")}
              description={tx("settings.models.configurationNameHelp", "Rename this saved model configuration.")}
            >
              <Input
                value={form.presetLabel}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, presetLabel: event.target.value }))
                }
                className="h-8 w-[min(280px,70vw)] rounded-full text-[13px]"
              />
            </SettingsRow>
          ) : null}
          <SettingsRow
            title={t("settings.rows.provider")}
            description={t("settings.help.provider")}
          >
            <ProviderPicker
              providers={providerOptions}
              value={providerValue}
              emptyLabel={t("settings.byok.noConfiguredProviders")}
              showProviderLogos={showBrandLogos}
              onChange={(provider) =>
                setForm((prev) => ({
                  ...prev,
                  provider,
                  model: provider === prev.provider ? prev.model : "",
                }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={t("settings.rows.model")}
            description={t("settings.help.model")}
          >
            <ModelIdPicker
              token={token}
              settings={settings}
              provider={form.provider}
              value={form.model}
              showProviderLogos={showBrandLogos}
              onChange={(model) => setForm((prev) => ({ ...prev, model }))}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.contextWindow", "Context window")}
            description={tx(
              "settings.help.contextWindow",
              "Choose the default context budget for this model configuration.",
            )}
          >
            <SegmentedControl
              value={String(form.contextWindowTokens)}
              options={CONTEXT_WINDOW_TOKEN_OPTIONS.map((tokens) => ({
                value: String(tokens),
                label: tokens === 262_144 ? "256K" : "64K",
              }))}
              onChange={(value) =>
                setForm((prev) => ({
                  ...prev,
                  contextWindowTokens: normalizeContextWindowTokens(Number(value)),
                }))
              }
            />
          </SettingsRow>
          <SettingsFooter
            dirty={dirty}
            saving={saving}
            saved={false}
            disabled={modelFieldsMissing}
            onSave={onSave}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

const PROVIDER_API_TYPES = ["auto", "chat_completions", "responses"] as const;

function ProvidersSettings({
  token,
  settings,
  expandedProvider,
  query,
  showBrandLogos,
  onQueryChange,
  onToggleProvider,
  onProviderSaved,
}: {
  token: string;
  settings: SettingsPayload;
  expandedProvider: string | null;
  query: string;
  showBrandLogos: boolean;
  onQueryChange: (query: string) => void;
  onToggleProvider: (provider: string) => void;
  onProviderSaved: (payload: SettingsPayload) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [draft, setDraft] = useState<{
    apiKey: string;
    apiBase: string;
    apiType: "auto" | "chat_completions" | "responses";
  } | null>(null);
  const [savingProvider, setSavingProvider] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const configuredProviders = settings.providers.filter((provider) => provider.configured);
  const unconfiguredProviders = useMemo(
    () => orderUnconfiguredProviders(settings.providers.filter((provider) => !provider.configured)),
    [settings.providers],
  );
  const filteredConfigured = filterProviders(configuredProviders, query);
  const filteredUnconfigured = filterProviders(unconfiguredProviders, query);

  // 切换展开的厂商时重置草稿与错误状态
  useEffect(() => {
    setDraft(null);
    setError(null);
    setSavingProvider(null);
  }, [expandedProvider]);

  const updateDraft = (provider: SettingsPayload["providers"][number]) => (
    patch: Partial<{ apiKey: string; apiBase: string; apiType: "auto" | "chat_completions" | "responses" }>,
  ) =>
    setDraft((current) => {
      const base = current ?? {
        apiKey: "",
        apiBase: provider.api_base || provider.default_api_base || "",
        apiType: (provider.api_type as "auto" | "chat_completions" | "responses") ?? "auto",
      };
      return { ...base, ...patch };
    });

  const saveProvider = async (provider: SettingsPayload["providers"][number]) => {
    if (!draft || savingProvider) return;
    const currentBase = provider.api_base || provider.default_api_base || "";
    const currentType = (provider.api_type as "auto" | "chat_completions" | "responses") ?? "auto";
    const apiKey = draft.apiKey.trim();
    const apiBase = draft.apiBase.trim();
    const hasKeyChange = Boolean(apiKey);
    const hasBaseChange = apiBase !== currentBase;
    const hasTypeChange = provider.name === "openai" && draft.apiType !== currentType;
    if (!hasKeyChange && !hasBaseChange && !hasTypeChange) {
      setDraft(null);
      return;
    }
    setSavingProvider(provider.name);
    setError(null);
    try {
      const payload = await updateProviderSettings(token, {
        provider: provider.name,
        ...(hasKeyChange ? { apiKey } : {}),
        ...(hasBaseChange ? { apiBase } : {}),
        ...(hasTypeChange ? { apiType: draft.apiType } : {}),
      });
      onProviderSaved(payload);
      setDraft(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSavingProvider(null);
    }
  };

  const renderProviderRow = (provider: SettingsPayload["providers"][number]) => {
    const expanded = expandedProvider === provider.name;
    const isOauthProvider = provider.auth_type === "oauth";
    const currentBase = provider.api_base || provider.default_api_base || "";
    const currentType = (provider.api_type as "auto" | "chat_completions" | "responses") ?? "auto";
    const baseValue = draft?.apiBase ?? currentBase;
    const typeValue = draft?.apiType ?? currentType;
    return (
      <div key={provider.name} className="divide-y divide-border/45">
        <button
          type="button"
          onClick={() => onToggleProvider(provider.name)}
          className="flex min-h-[70px] w-full items-center justify-between gap-4 px-4 py-3 text-left transition-colors hover:bg-muted/35 sm:px-5"
        >
          <span className="flex min-w-0 items-center gap-3">
            <ProviderIcon
              provider={provider.name}
              showBrandLogos={showBrandLogos}
            />
            <span className="min-w-0">
              <span className="block truncate text-[15px] font-semibold leading-5 text-foreground">
                {provider.label}
              </span>
              <span className="block truncate text-[12px] text-muted-foreground">
                {provider.api_base || provider.default_api_base || provider.name}
              </span>
            </span>
          </span>
          <StatusPill tone={provider.configured ? "success" : "neutral"}>
            {isOauthProvider
              ? provider.configured
                ? tx("settings.oauth.signedIn", "Signed in")
                : tx("settings.oauth.notSignedIn", "Not signed in")
              : provider.configured
                ? t("settings.byok.configured")
                : t("settings.byok.notConfigured")}
          </StatusPill>
        </button>

        {expanded && !isOauthProvider ? (
          <div className="space-y-3 rounded-[18px] border border-border/45 bg-background/75 px-4 py-3 sm:px-5">
            <div>
              <p className="text-[12px] font-medium text-muted-foreground">
                {tx("settings.providers.apiBase", "API 地址")}
              </p>
              <Input
                value={baseValue}
                onChange={(event) => updateDraft(provider)({ apiBase: event.target.value })}
                placeholder={provider.default_api_base || "https://api.example.com/v1"}
                className="mt-1 h-9 rounded-full text-[13px]"
              />
            </div>
            <div>
              <p className="text-[12px] font-medium text-muted-foreground">
                {tx("settings.providers.apiKey", "API Key")}
              </p>
              <Input
                type="password"
                value={draft?.apiKey ?? ""}
                onChange={(event) => updateDraft(provider)({ apiKey: event.target.value })}
                placeholder={
                  provider.api_key_hint
                    ? `${tx("settings.providers.currentKey", "当前")}: ${provider.api_key_hint}`
                    : tx("settings.providers.apiKeyPlaceholder", "留空保持不变")
                }
                className="mt-1 h-9 rounded-full text-[13px]"
              />
            </div>
            {provider.name === "openai" ? (
              <div>
                <p className="text-[12px] font-medium text-muted-foreground">
                  {tx("settings.providers.apiType", "API 形态")}
                </p>
                <div className="mt-1 flex flex-wrap gap-2">
                  {PROVIDER_API_TYPES.map((apiType) => (
                    <button
                      key={apiType}
                      type="button"
                      onClick={() => updateDraft(provider)({ apiType })}
                      className={cn(
                        "h-8 rounded-full border px-3 text-[12px] transition-colors",
                        typeValue === apiType
                          ? "border-primary bg-primary/10 text-primary"
                          : "border-input text-muted-foreground hover:bg-muted/50",
                      )}
                    >
                      {apiType}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
            {error ? (
              <p className="text-[12px] text-destructive">{error}</p>
            ) : null}
            <div className="flex items-center gap-2 pt-1">
              <Button
                type="button"
                size="sm"
                disabled={savingProvider === provider.name}
                onClick={() => saveProvider(provider)}
              >
                {savingProvider === provider.name
                  ? tx("settings.providers.saving", "保存中…")
                  : tx("settings.providers.save", "保存")}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => onToggleProvider(provider.name)}
              >
                {tx("settings.providers.cancel", "取消")}
              </Button>
            </div>
          </div>
        ) : expanded && isOauthProvider ? (
          <div className="space-y-3 rounded-[18px] border border-border/45 bg-background/75 px-4 py-3 sm:px-5">
            <div>
              <p className="text-[12px] font-medium text-muted-foreground">
                {tx("settings.providers.keyStatus", "密钥状态")}
              </p>
              <p className="mt-1 text-[13px] text-foreground">
                {provider.api_key_hint ??
                  (provider.configured
                    ? t("settings.byok.configured")
                    : t("settings.byok.notConfigured"))}
              </p>
            </div>
          </div>
        ) : null}
      </div>
    );
  };
  return (
    <div className="space-y-6">
      <p className="max-w-[42rem] text-[13px] leading-6 text-muted-foreground">
        {tx(
          "settings.providers.description",
          "配置各厂商的 API Key 与 API 地址。配置后，文生图 / TTS / 语音转写等页面即可选择该厂商并拉取其模型列表。",
        )}
      </p>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={tx("settings.providers.searchPlaceholder", "Search providers")}
          className="h-10 rounded-full pl-9 text-[13px]"
        />
      </div>
      <ProviderSection
        title={t("settings.byok.configuredSection")}
        count={filteredConfigured.length}
        empty={t("settings.byok.noConfiguredProviders")}
      >
        {filteredConfigured.map(renderProviderRow)}
      </ProviderSection>
      <ProviderSection
        title={t("settings.byok.notConfiguredSection")}
        count={filteredUnconfigured.length}
        empty={tx("settings.providers.noMatches", "No providers match this search.")}
      >
        {filteredUnconfigured.map(renderProviderRow)}
      </ProviderSection>
      <ThirdPartyBrandNotice />
    </div>
  );
}

function ImageGenerationSettings({
  token,
  settings,
  form,
  dirty,
  saving,
  onChangeForm,
  onSave,
  onOpenProviders,
  showBrandLogos,
  onRestart,
  isRestarting,
  requiresRestartPending,
}: {
  token: string;
  settings: SettingsPayload;
  form: ImageGenerationSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<ImageGenerationSettingsUpdate>>;
  onSave: () => void;
  onOpenProviders: () => void;
  showBrandLogos: boolean;
  onRestart?: () => void;
  isRestarting?: boolean;
  requiresRestartPending: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const imageProviders = providersWithCapability(settings, "image");
  const selectedProvider =
    imageProviders.find((provider) => provider.name === form.provider) ?? imageProviders[0];
  const providerConfigured = !!selectedProvider?.configured;
  const missingCredential = form.enabled && !providerConfigured;
  const aspectOptions = optionRowsWithCurrent(
    IMAGE_ASPECT_RATIO_OPTIONS.map((value) => ({ name: value, label: value })),
    form.defaultAspectRatio,
  );
  const sizeOptions = optionRowsWithCurrent(
    IMAGE_SIZE_OPTIONS.map((value) => ({ name: value, label: value })),
    form.defaultImageSize,
  );

  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{tx("settings.sections.imageGeneration", "Image generation")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.imageGeneration", "Image generation")}
            description={tx("settings.help.imageGeneration", "Expose generate_image in chats when a configured image provider is available.")}
          >
            <ToggleButton
              checked={form.enabled}
              onChange={(enabled) => onChangeForm((prev) => ({ ...prev, enabled }))}
              ariaLabel={tx("settings.rows.imageGeneration", "Image generation")}
              label={form.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.imageProvider", "Image provider")}
            description={tx("settings.help.imageProvider", "Choose the registry provider used by generate_image.")}
          >
            <ProviderPicker
              providers={imageProviders}
              value={form.provider}
              emptyLabel={tx("settings.image.selectProvider", "Select provider")}
              showProviderLogos={showBrandLogos}
              onChange={(provider) => onChangeForm((prev) => ({ ...prev, provider }))}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.imageProviderStatus", "Provider status")}
            description={tx("settings.help.imageProviderStatus", "Image generation reuses provider credentials from Providers.")}
          >
            <div className="flex flex-wrap items-center justify-end gap-2">
              <StatusPill tone={providerConfigured ? "success" : "neutral"}>
                {providerConfigured
                  ? tx("settings.values.configured", "Configured")
                  : tx("settings.values.notConfigured", "Not configured")}
              </StatusPill>
              {!providerConfigured ? (
                <Button size="sm" variant="outline" onClick={onOpenProviders} className="rounded-full">
                  {tx("settings.image.configureProvider", "Configure provider")}
                </Button>
              ) : null}
            </div>
        </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.imageDefaults", "Defaults")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.imageModel", "Image model")}
            description={tx("settings.help.imageModel", "Model name sent to the selected image provider.")}
          >
            <ModelIdPicker
              token={token}
              settings={settings}
              provider={form.provider}
              value={form.model}
              showProviderLogos={showBrandLogos}
              onChange={(model) => onChangeForm((prev) => ({ ...prev, model }))}
              providerRows={imageProviders}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.defaultAspectRatio", "Default aspect")}
            description={tx("settings.help.defaultAspectRatio", "Used when the prompt does not choose an aspect ratio.")}
          >
            <ProviderPicker
              providers={aspectOptions}
              value={form.defaultAspectRatio}
              emptyLabel={tx("settings.image.selectAspect", "Select aspect")}
              onChange={(defaultAspectRatio) =>
                onChangeForm((prev) => ({ ...prev, defaultAspectRatio }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.defaultImageSize", "Default size")}
            description={tx("settings.help.defaultImageSize", "Size hint sent to providers that support it.")}
          >
            <ProviderPicker
              providers={sizeOptions}
              value={form.defaultImageSize}
              emptyLabel={tx("settings.image.selectSize", "Select size")}
              onChange={(defaultImageSize) =>
                onChangeForm((prev) => ({ ...prev, defaultImageSize }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.maxImagesPerTurn", "Max images per turn")}
            description={tx("settings.help.maxImagesPerTurn", "Upper bound for one generate_image request.")}
          >
            <NumberInput
              value={form.maxImagesPerTurn}
              min={1}
              max={8}
              onChange={(maxImagesPerTurn) =>
                onChangeForm((prev) => ({ ...prev, maxImagesPerTurn }))
              }
            />
          </SettingsRow>
          <ReadOnlyRow title={tx("settings.rows.imageSaveDir", "Save directory")} value={settings.image_generation.save_dir} />
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            disabled={missingCredential}
            message={
              missingCredential
                ? tx("settings.image.missingCredential", "Configure this provider before enabling image generation.")
                : undefined
            }
            dirtyMessage={tx("settings.status.restartAfterSaving", "Save changes, then restart when ready.")}
            pendingMessage={tx("settings.status.savedRestartApply", "Saved. Restart when ready.")}
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

function VideoGenerationSettings({
  settings,
  form,
  dirty,
  saving,
  onChangeForm,
  onSave,
  onOpenProviders,
  onRestart,
  isRestarting,
  requiresRestartPending,
}: {
  settings: SettingsPayload;
  form: VideoGenerationSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<VideoGenerationSettingsUpdate>>;
  onSave: () => void;
  onOpenProviders: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
  requiresRestartPending: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const volcengineProvider = settings.providers.find((provider) => provider.name === "volcengine");
  const apiKeyConfigured = settings.video_generation.api_key_configured || !!volcengineProvider?.configured;
  const missingCredential = form.enabled && !apiKeyConfigured;
  const ratioOptions = optionRowsWithCurrent(
    VIDEO_RATIO_OPTIONS.map((value) => ({ name: value, label: value })),
    form.defaultRatio,
  );
  const resolutionOptions = optionRowsWithCurrent(
    [
      { name: "", label: tx("settings.video.resolutionAuto", "模型自动决定") },
      ...VIDEO_RESOLUTION_OPTIONS.map((value) => ({ name: value, label: value })),
    ],
    form.defaultResolution,
  );
  const modelOptions = optionRowsWithCurrent(
    SEEDANCE_MODELS.map((value) => ({ name: value, label: value })),
    form.model,
  );

  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{tx("settings.sections.videoGeneration", "视频生成")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.videoGeneration", "视频生成")}
            description={tx("settings.help.videoGeneration", "在对话中暴露视频生成能力（当 Seedance 密钥已配置时）。")}
          >
            <ToggleButton
              checked={form.enabled}
              onChange={(enabled) => onChangeForm((prev) => ({ ...prev, enabled }))}
              ariaLabel={tx("settings.rows.videoGeneration", "视频生成")}
              label={form.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.videoProviderStatus", "密钥状态")}
            description={tx("settings.help.videoProviderStatus", "文生视频与文生图共用「模型厂商」页的火山方舟密钥。")}
          >
            <div className="flex flex-wrap items-center justify-end gap-2">
              <StatusPill tone={apiKeyConfigured ? "success" : "neutral"}>
                {apiKeyConfigured
                  ? tx("settings.values.configured", "已配置")
                  : tx("settings.values.notConfigured", "未配置")}
              </StatusPill>
              {!apiKeyConfigured ? (
                <Button size="sm" variant="outline" onClick={onOpenProviders} className="rounded-full">
                  {tx("settings.video.configureProvider", "配置厂商")}
                </Button>
              ) : null}
            </div>
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.videoDefaults", "生成参数")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.videoModel", "视频模型")}
            description={tx("settings.help.videoModel", "发送给 Seedance 的视频模型名称。")}
          >
            <div className="flex flex-wrap items-center justify-end gap-2">
              <ProviderPicker
                providers={modelOptions}
                value={form.model}
                emptyLabel={tx("settings.video.selectModel", "选择视频模型")}
                onChange={(model) => onChangeForm((prev) => ({ ...prev, model }))}
              />
              <Input
                value={form.model}
                onChange={(event) => onChangeForm((prev) => ({ ...prev, model: event.target.value }))}
                placeholder={tx("settings.video.modelPlaceholder", "或手动输入模型 ID")}
                className="h-8 w-[min(260px,60vw)] rounded-full text-[13px]"
              />
            </div>
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.videoRatio", "画面比例")}
            description={tx("settings.help.videoRatio", "提示未指定时使用的默认画面比例。")}
          >
            <ProviderPicker
              providers={ratioOptions}
              value={form.defaultRatio}
              emptyLabel={tx("settings.video.selectRatio", "选择画面比例")}
              onChange={(defaultRatio) => onChangeForm((prev) => ({ ...prev, defaultRatio }))}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.videoDuration", "时长")}
            description={tx("settings.help.videoDuration", "生成的视频时长（秒），4–30。")}
          >
            <NumberInput
              value={form.defaultDuration}
              min={4}
              max={30}
              onChange={(defaultDuration) =>
                onChangeForm((prev) => ({ ...prev, defaultDuration }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.videoResolution", "分辨率")}
            description={tx("settings.help.videoResolution", "提示未指定时使用的默认分辨率；「模型自动决定」交由模型选择。")}
          >
            <ProviderPicker
              providers={resolutionOptions}
              value={form.defaultResolution}
              emptyLabel={tx("settings.video.selectResolution", "选择分辨率")}
              onChange={(defaultResolution) =>
                onChangeForm((prev) => ({ ...prev, defaultResolution }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.videoGenerateAudio", "生成音轨")}
            description={tx("settings.help.videoGenerateAudio", "生成带音轨的视频。")}
          >
            <ToggleButton
              checked={form.generateAudio}
              onChange={(generateAudio) =>
                onChangeForm((prev) => ({ ...prev, generateAudio }))
              }
              ariaLabel={tx("settings.rows.videoGenerateAudio", "生成音轨")}
              label={form.generateAudio ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.videoWatermark", "水印")}
            description={tx("settings.help.videoWatermark", "为生成的视频添加水印。")}
          >
            <ToggleButton
              checked={form.watermark}
              onChange={(watermark) => onChangeForm((prev) => ({ ...prev, watermark }))}
              ariaLabel={tx("settings.rows.videoWatermark", "水印")}
              label={form.watermark ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <ReadOnlyRow
            title={tx("settings.rows.videoSaveDir", "保存目录")}
            value={settings.video_generation.save_dir}
          />
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            disabled={missingCredential}
            message={
              missingCredential
                ? tx(
                    "settings.video.missingCredential",
                    "在 config.json 配置 Seedance 密钥后再启用视频生成。",
                  )
                : undefined
            }
            dirtyMessage={tx("settings.status.restartAfterSaving", "保存更改，就绪后重启。")}
            pendingMessage={tx("settings.status.savedRestartApply", "已保存。就绪后重启。")}
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

function VisionSettings({
  settings,
  form,
  dirty,
  saving,
  onChangeForm,
  onSave,
  onOpenModels,
  onRestart,
  isRestarting,
  requiresRestartPending,
}: {
  settings: SettingsPayload;
  form: ScreenshotSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<ScreenshotSettingsUpdate>>;
  onSave: () => void;
  onOpenModels: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
  requiresRestartPending: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const visionProviders = providersWithCapability(settings, "vision");
  const visionModelConfigured = !!(
    form.visionModel &&
    visionProviders.some(
      (provider) => provider.name === form.visionModel && provider.configured,
    )
  );
  const missingVisionModel = form.enabled && !form.visionModel;
  const visionProviderOptions = visionProviders.map(
    (provider) => ({
      name: provider.name,
      label: provider.configured
        ? provider.label
        : `${provider.label} (${tx("settings.values.notConfigured", "Not configured")})`,
    }),
  );
  const selectedPreset =
    settings.model_presets.find((preset) => preset.name === form.visionModel) ?? null;
  const overrideTrimmed = (form.visionModelOverride ?? "").trim();
  const resolvedModel = overrideTrimmed || selectedPreset?.model || "";

  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{tx("settings.sections.vision", "Vision")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.screenshot", "Screenshot")}
            description={tx("settings.help.screenshot", "Allow the agent to capture screenshots to understand the current screen state.")}
          >
            <ToggleButton
              checked={form.enabled}
              onChange={(enabled) => onChangeForm((prev) => ({ ...prev, enabled }))}
              ariaLabel={tx("settings.rows.screenshot", "Screenshot")}
              label={form.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.visionModelOverride", "Vision model")}
            description={tx("settings.help.visionModelOverride", "Multimodal model id used to interpret screenshots (e.g. gpt-4o, qwen-vl-max, claude-3-5-sonnet). Leave empty to use the preset's default model.")}
          >
            <Input
              value={form.visionModelOverride ?? ""}
              onChange={(event) =>
                onChangeForm((prev) => ({ ...prev, visionModelOverride: event.target.value || null }))
              }
              placeholder={selectedPreset?.model ?? tx("settings.vision.overridePlaceholder", "e.g. gpt-4o")}
              className="h-8 w-[min(300px,70vw)] rounded-full text-[13px]"
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.visionModel", "Provider credentials")}
            description={tx("settings.help.visionModel", "Select a provider to supply the API key and endpoint for the vision model above.")}
          >
            <div className="flex flex-wrap items-center justify-end gap-2">
              <ProviderPicker
                providers={visionProviderOptions}
                value={form.visionModel ?? ""}
                emptyLabel={tx("settings.vision.selectProvider", "Select provider")}
                onChange={(visionModel) =>
                  onChangeForm((prev) => ({ ...prev, visionModel }))
                }
              />
              <StatusPill tone={visionModelConfigured ? "success" : "neutral"}>
                {visionModelConfigured
                  ? tx("settings.values.configured", "Configured")
                  : tx("settings.values.notConfigured", "Not configured")}
              </StatusPill>
              {!visionModelConfigured ? (
                <Button size="sm" variant="outline" onClick={onOpenModels} className="rounded-full">
                  {tx("settings.vision.configureModel", "Configure model")}
                </Button>
              ) : null}
            </div>
          </SettingsRow>
          {resolvedModel ? (
            <ReadOnlyRow
              title={tx("settings.rows.visionModelId", "Resolved model")}
              value={resolvedModel}
              description={overrideTrimmed
                ? tx("settings.help.visionModelIdOverride", "Multimodal model id that will be sent to the provider.")
                : tx("settings.help.visionModelId", "Underlying model id of the selected preset.")}
            />
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.visionCapture", "Capture")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.screenshotMaxWidth", "Max width")}
            description={tx("settings.help.screenshotMaxWidth", "Downscale captured screenshots to this max width (pixels).")}
          >
            <NumberInput
              value={form.maxWidth}
              min={320}
              max={7680}
              onChange={(maxWidth) => onChangeForm((prev) => ({ ...prev, maxWidth }))}
              suffix="px"
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.screenshotMaxHeight", "Max height")}
            description={tx("settings.help.screenshotMaxHeight", "Downscale captured screenshots to this max height (pixels).")}
          >
            <NumberInput
              value={form.maxHeight}
              min={240}
              max={4320}
              onChange={(maxHeight) => onChangeForm((prev) => ({ ...prev, maxHeight }))}
              suffix="px"
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.screenshotQuality", "JPEG quality")}
            description={tx("settings.help.screenshotQuality", "Compression quality for the JPEG sent to the vision model (10-100).")}
          >
            <NumberInput
              value={form.quality}
              min={10}
              max={100}
              onChange={(quality) => onChangeForm((prev) => ({ ...prev, quality }))}
            />
          </SettingsRow>
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            disabled={missingVisionModel}
            message={
              missingVisionModel
                ? tx("settings.vision.missingModel", "Select a vision model before enabling screenshots.")
                : undefined
            }
            dirtyMessage={tx("settings.status.restartAfterSaving", "Save changes, then restart when ready.")}
            pendingMessage={tx("settings.status.savedRestartApply", "Saved. Restart when ready.")}
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

function SystemIoSettings({
  settings,
  form,
  dirty,
  saving,
  onChangeForm,
  onSave,
  onRestart,
  isRestarting,
  requiresRestartPending,
}: {
  settings: SettingsPayload;
  form: SystemIoSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<SystemIoSettingsUpdate>>;
  onSave: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
  requiresRestartPending: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const actions = settings.system_io.available_actions;
  const readActions = actions.filter((a) => !a.write);
  const writeActions = actions.filter((a) => a.write);
  const allPermitted = form.allowActions.length === 0;

  const isActionAllowed = (name: string) =>
    allPermitted || form.allowActions.includes(name);

  const toggleAction = (name: string) => {
    onChangeForm((prev) => {
      if (prev.allowActions.length === 0) {
        // Transitioning from "all permitted" to restricted: start with all
        // actions, then remove the clicked one so other toggles stay on.
        return {
          ...prev,
          allowActions: actions.filter((a) => a.name !== name).map((a) => a.name),
        };
      }
      const has = prev.allowActions.includes(name);
      return {
        ...prev,
        allowActions: has
          ? prev.allowActions.filter((a) => a !== name)
          : [...prev.allowActions, name],
      };
    });
  };

  const renderActionRow = (action: { name: string; label: string; write: boolean }) => (
    <SettingsRow key={action.name} title={action.label} description={action.name}>
      <ToggleButton
        checked={isActionAllowed(action.name)}
        onChange={() => toggleAction(action.name)}
        ariaLabel={action.label}
        label={isActionAllowed(action.name) ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
      />
    </SettingsRow>
  );

  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{tx("settings.sections.systemIo", "System IO")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.systemIo", "System IO")}
            description={tx("settings.help.systemIo", "Allow the agent to simulate keyboard/mouse input, read/write the clipboard, list USB devices, and read/write serial ports on the host OS.")}
          >
            <ToggleButton
              checked={form.enabled}
              onChange={(enabled) => onChangeForm((prev) => ({ ...prev, enabled }))}
              ariaLabel={tx("settings.rows.systemIo", "System IO")}
              label={form.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.systemIoActions", "Actions")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.systemIoAllowActions", "Permitted actions")}
            description={tx("settings.help.systemIoAllowActions", "When no actions are selected, all actions are permitted. Toggle specific actions off to restrict to a subset (recommended for write actions).")}
          >
            <StatusPill tone={allPermitted ? "warning" : "neutral"}>
              {allPermitted
                ? tx("settings.values.systemIoAllActions", "All permitted")
                : tx("settings.values.systemIoRestricted", "Restricted")}
            </StatusPill>
          </SettingsRow>
          {readActions.map(renderActionRow)}
          {writeActions.map(renderActionRow)}
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            dirtyMessage={tx("settings.status.restartAfterSaving", "Save changes, then restart when ready.")}
            pendingMessage={tx("settings.status.savedRestartApply", "Saved. Restart when ready.")}
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

function TranscriptionSettings({
  token,
  settings,
  form,
  dirty,
  saving,
  onChangeForm,
  onSave,
  onOpenProviders,
  showBrandLogos,
  onRestart,
  isRestarting,
  requiresRestartPending,
}: {
  token: string;
  settings: SettingsPayload;
  form: TranscriptionSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<TranscriptionSettingsUpdate>>;
  onSave: () => void;
  onOpenProviders: () => void;
  showBrandLogos: boolean;
  onRestart?: () => void;
  isRestarting?: boolean;
  requiresRestartPending: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const transcriptionProviders = providersWithCapability(settings, "transcription");
  const selectedProvider =
    transcriptionProviders.find((provider) => provider.name === form.provider) ??
    transcriptionProviders[0];
  const providerConfigured = !!selectedProvider?.configured;

  return (
    <section>
      <SettingsSectionTitle>{tx("settings.sections.voiceInput", "Voice input")}</SettingsSectionTitle>
      <SettingsGroup>
        <SettingsRow
          title={tx("settings.rows.transcription", "Transcription")}
          description={tx("settings.help.transcription", "Transcribe microphone input before sending it. Chat channel voice messages use the same settings.")}
        >
          <ToggleButton
            checked={form.enabled}
            onChange={(enabled) => onChangeForm((prev) => ({ ...prev, enabled }))}
            ariaLabel={tx("settings.rows.transcription", "Transcription")}
            label={form.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
          />
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.transcriptionProvider", "Provider")}
          description={tx("settings.help.transcriptionProvider", "Uses the matching provider credentials from Providers.")}
        >
          <ProviderPicker
            providers={transcriptionProviders}
            value={form.provider}
            emptyLabel={tx("settings.voice.selectProvider", "Select provider")}
            showProviderLogos={showBrandLogos}
            onChange={(provider) => onChangeForm((prev) => ({ ...prev, provider }))}
          />
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.transcriptionProviderStatus", "Provider status")}
          description={tx("settings.help.transcriptionProviderStatus", "API keys stay under providers, not in transcription settings.")}
        >
          <div className="flex flex-wrap items-center justify-end gap-2">
            <StatusPill tone={providerConfigured ? "success" : "neutral"}>
              {providerConfigured
                ? tx("settings.values.configured", "Configured")
                : tx("settings.values.notConfigured", "Not configured")}
            </StatusPill>
            {!providerConfigured ? (
              <Button size="sm" variant="outline" onClick={onOpenProviders} className="rounded-full">
                {tx("settings.voice.configureProvider", "Configure provider")}
              </Button>
            ) : null}
          </div>
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.transcriptionModel", "Model")}
          description={tx("settings.help.transcriptionModel", "Leave as the resolved default unless your provider needs a custom model id.")}
        >
          <ModelIdPicker
            token={token}
            settings={settings}
            provider={form.provider}
            value={form.model}
            showProviderLogos={showBrandLogos}
            onChange={(model) => onChangeForm((prev) => ({ ...prev, model }))}
            providerRows={transcriptionProviders}
          />
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.transcriptionLanguage", "Language")}
          description={tx("settings.help.transcriptionLanguage", "Optional ISO-639 hint such as en, zh, ja, or ko.")}
        >
          <Input
            value={form.language}
            onChange={(event) => onChangeForm((prev) => ({ ...prev, language: event.target.value }))}
            placeholder={tx("settings.voice.languageAuto", "Auto")}
            className="h-8 w-[min(180px,60vw)] rounded-full text-[13px]"
          />
        </SettingsRow>
        <SettingsRow title={tx("settings.rows.voiceLimits", "Limits")}>
          <div className="flex flex-wrap justify-end gap-2">
            <NumberInput
              value={form.maxDurationSec}
              min={1}
              max={600}
              suffix="s"
              onChange={(maxDurationSec) => onChangeForm((prev) => ({ ...prev, maxDurationSec }))}
            />
            <NumberInput
              value={form.maxUploadMb}
              min={1}
              max={100}
              suffix="MB"
              onChange={(maxUploadMb) => onChangeForm((prev) => ({ ...prev, maxUploadMb }))}
            />
          </div>
        </SettingsRow>
        <RestartSettingsFooter
          dirty={dirty}
          saving={saving}
          pendingRestart={requiresRestartPending}
          dirtyMessage={tx("settings.status.restartAfterSaving", "Save changes, then restart when ready.")}
          pendingMessage={tx("settings.status.savedRestartApply", "Saved. Restart when ready.")}
          onSave={onSave}
          onRestart={onRestart}
          isRestarting={isRestarting}
        />
      </SettingsGroup>
    </section>
  );
}

function TtsSettings({
  token,
  settings,
  form,
  dirty,
  saving,
  onChangeForm,
  onSave,
  onOpenProviders,
  showBrandLogos,
}: {
  token: string;
  settings: SettingsPayload;
  form: TtsSettingsUpdate;
  dirty: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<TtsSettingsUpdate>>;
  onSave: () => void;
  onOpenProviders: () => void;
  showBrandLogos: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const ttsProviders = providersWithCapability(settings, "tts");
  const selectedProvider =
    ttsProviders.find((provider) => provider.name === form.provider) ??
    ttsProviders[0];
  const providerConfigured = !!selectedProvider?.configured;

  return (
    <section>
      <SettingsSectionTitle>{tx("settings.sections.tts", "Text to speech")}</SettingsSectionTitle>
      <SettingsGroup>
        <SettingsRow
          title={tx("settings.rows.tts", "Text-to-speech")}
          description={tx("settings.help.tts", "Synthesize speech audio from text with the text_to_speech tool.")}
        >
          <ToggleButton
            checked={form.enabled}
            onChange={(enabled) => onChangeForm((prev) => ({ ...prev, enabled }))}
            ariaLabel={tx("settings.rows.tts", "Text-to-speech")}
            label={form.enabled ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
          />
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.ttsProvider", "Provider")}
          description={tx("settings.help.ttsProvider", "Uses the matching provider credentials from Providers.")}
        >
          <ProviderPicker
            providers={ttsProviders}
            value={form.provider}
            emptyLabel={tx("settings.voice.selectProvider", "Select provider")}
            showProviderLogos={showBrandLogos}
            onChange={(provider) => onChangeForm((prev) => ({ ...prev, provider }))}
          />
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.ttsProviderStatus", "Provider status")}
          description={tx("settings.help.ttsProviderStatus", "API keys stay under providers, not in TTS settings. edge-tts needs no key.")}
        >
          <div className="flex flex-wrap items-center justify-end gap-2">
            <StatusPill tone={providerConfigured ? "success" : "neutral"}>
              {providerConfigured
                ? tx("settings.values.configured", "Configured")
                : tx("settings.values.notConfigured", "Not configured")}
            </StatusPill>
            {!providerConfigured ? (
              <Button size="sm" variant="outline" onClick={onOpenProviders} className="rounded-full">
                {tx("settings.voice.configureProvider", "Configure provider")}
              </Button>
            ) : null}
          </div>
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.ttsModel", "Model")}
          description={tx("settings.help.ttsModel", "Leave as the resolved default unless your provider needs a custom model id.")}
        >
          <ModelIdPicker
            token={token}
            settings={settings}
            provider={form.provider}
            value={form.model}
            showProviderLogos={showBrandLogos}
            onChange={(model) => onChangeForm((prev) => ({ ...prev, model }))}
            providerRows={ttsProviders}
          />
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.ttsVoice", "Voice")}
          description={tx("settings.help.ttsVoice", "Default voice for the provider, e.g. alloy, longxiaochun or zh-CN-XiaoxiaoNeural.")}
        >
          <Input
            value={form.voice}
            onChange={(event) => onChangeForm((prev) => ({ ...prev, voice: event.target.value }))}
            className="h-8 w-[min(300px,70vw)] rounded-full text-[13px]"
          />
        </SettingsRow>
        <SettingsRow
          title={tx("settings.rows.ttsRate", "Rate")}
          description={tx("settings.help.ttsRate", "Optional speed such as +10% (faster) or -20% (slower).")}
        >
          <Input
            value={form.rate}
            onChange={(event) => onChangeForm((prev) => ({ ...prev, rate: event.target.value }))}
            placeholder={tx("settings.voice.languageAuto", "Auto")}
            className="h-8 w-[min(180px,60vw)] rounded-full text-[13px]"
          />
        </SettingsRow>
        <RestartSettingsFooter
          dirty={dirty}
          saving={saving}
          pendingRestart={false}
          dirtyMessage={tx("settings.status.unsavedTts", "Save changes — applies immediately, no restart needed.")}
          pendingMessage={tx("settings.status.savedTts", "Saved.")}
          onSave={onSave}
        />
      </SettingsGroup>
    </section>
  );
}

function WebSettings({
  settings,
  form,
  keyVisible,
  keyEditing,
  saving,
  onChangeForm,
  onChangeProvider,
  onToggleKey,
  onToggleKeyEditing,
  onReset,
  onSave,
  showBrandLogos,
  onRestart,
  isRestarting,
  requiresRestartPending,
}: {
  settings: SettingsPayload;
  form: WebSearchSettingsUpdate;
  keyVisible: boolean;
  keyEditing: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<WebSearchSettingsUpdate>>;
  onChangeProvider: (provider: string) => void;
  onToggleKey: () => void;
  onToggleKeyEditing: () => void;
  onReset: () => void;
  onSave: () => void;
  showBrandLogos: boolean;
  onRestart?: () => void;
  isRestarting?: boolean;
  requiresRestartPending: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const selectedProvider =
    settings.web_search.providers.find((provider) => provider.name === form.provider) ??
    settings.web_search.providers[0];
  const hasExistingSecret =
    selectedProvider?.credential === "api_key" &&
    form.provider === settings.web_search.provider &&
    !!settings.web_search.api_key_hint;
  const showKeyInput = selectedProvider?.credential === "api_key" && (!hasExistingSecret || keyEditing);
  const apiKey = form.apiKey?.trim() ?? "";
  const baseUrl = form.baseUrl?.trim() ?? "";
  const effectiveJinaReader = form.useJinaReader ?? settings.web.fetch.use_jina_reader;
  const dirty =
    form.provider !== settings.web_search.provider ||
    apiKey.length > 0 ||
    baseUrl !== (settings.web_search.base_url ?? "") ||
    form.maxResults !== settings.web_search.max_results ||
    form.timeout !== settings.web_search.timeout ||
    effectiveJinaReader !== settings.web.fetch.use_jina_reader;
  const jinaReaderDirty = effectiveJinaReader !== settings.web.fetch.use_jina_reader;
  const missingCredential =
    selectedProvider?.credential === "api_key"
      ? !apiKey && !hasExistingSecret
      : selectedProvider?.credential === "base_url"
        ? !baseUrl
        : false;

  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{tx("settings.sections.webSearch", "Web search")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.byok.webSearch.provider")}
            description={t("settings.byok.webSearch.providerHelp")}
          >
            <ProviderPicker
              providers={settings.web_search.providers}
              value={form.provider}
              emptyLabel={t("settings.byok.webSearch.selectProvider")}
              showProviderLogos={showBrandLogos}
              onChange={onChangeProvider}
            />
          </SettingsRow>

          {selectedProvider?.credential === "none" ? (
            <SettingsRow
              title={t("settings.byok.webSearch.credentials")}
              description={t("settings.byok.webSearch.noCredentialHelp")}
            >
              <StatusPill tone="success">{t("settings.byok.webSearch.noCredentialRequired")}</StatusPill>
            </SettingsRow>
          ) : null}

          {selectedProvider?.credential === "api_key" ? (
            <SettingsRow
              title={t("settings.byok.apiKey")}
              description={t("settings.byok.webSearch.apiKeyHelp")}
            >
              <div className="relative w-[280px] max-w-full">
                {showKeyInput ? (
                  <>
                    <Input
                      type={keyVisible ? "text" : "password"}
                      value={form.apiKey ?? ""}
                      onChange={(event) =>
                        onChangeForm((prev) => ({ ...prev, apiKey: event.target.value }))
                      }
                      placeholder={
                        hasExistingSecret
                          ? t("settings.byok.apiKeyConfiguredPlaceholder")
                          : t("settings.byok.apiKeyPlaceholder")
                      }
                      className="h-9 rounded-full pr-11 text-[13px]"
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={onToggleKey}
                      aria-label={
                        keyVisible ? t("settings.byok.hideApiKey") : t("settings.byok.showApiKey")
                      }
                      className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      {keyVisible ? (
                        <EyeOff className="h-3.5 w-3.5" aria-hidden />
                      ) : (
                        <Eye className="h-3.5 w-3.5" aria-hidden />
                      )}
                    </Button>
                  </>
                ) : (
                  <>
                    <div className="flex h-9 items-center rounded-full border border-input bg-background px-3 pr-11 text-[13px] text-muted-foreground">
                      {settings.web_search.api_key_hint ?? t("settings.byok.configuredKeyHint")}
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={onToggleKeyEditing}
                      aria-label={t("settings.actions.edit")}
                      className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      <Pencil className="h-3.5 w-3.5" aria-hidden />
                    </Button>
                  </>
                )}
              </div>
            </SettingsRow>
          ) : null}

          {selectedProvider?.credential === "base_url" ? (
            <SettingsRow
              title={t("settings.byok.webSearch.baseUrl")}
              description={t("settings.byok.webSearch.baseUrlHelp")}
            >
              <Input
                value={form.baseUrl ?? ""}
                onChange={(event) =>
                  onChangeForm((prev) => ({ ...prev, baseUrl: event.target.value }))
                }
                placeholder={t("settings.byok.webSearch.baseUrlPlaceholder")}
                className="h-9 w-[280px] rounded-full text-[13px]"
              />
            </SettingsRow>
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{tx("settings.sections.webBehavior", "Behavior")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.maxResults", "Max results")}
            description={tx("settings.help.maxResults", "Results returned by each web_search call.")}
          >
            <NumberInput
              value={form.maxResults ?? settings.web_search.max_results}
              min={1}
              max={10}
              onChange={(maxResults) => onChangeForm((prev) => ({ ...prev, maxResults }))}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.timeout", "Timeout")}
            description={tx("settings.help.timeout", "Seconds before a search provider request times out.")}
          >
            <NumberInput
              value={form.timeout ?? settings.web_search.timeout}
              min={1}
              max={120}
              onChange={(timeout) => onChangeForm((prev) => ({ ...prev, timeout }))}
              suffix="s"
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.jinaReader", "Jina reader")}
            description={tx("settings.help.jinaReader", "Use Jina Reader for web_fetch when available.")}
          >
            <ToggleButton
              checked={effectiveJinaReader}
              onChange={(useJinaReader) => onChangeForm((prev) => ({ ...prev, useJinaReader }))}
              ariaLabel={tx("settings.rows.jinaReader", "Jina reader")}
              label={effectiveJinaReader ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            disabled={missingCredential}
            message={
              missingCredential
                ? t("settings.byok.webSearch.missingCredential")
                : requiresRestartPending && !dirty
                  ? tx("settings.status.savedRestartApply", "Saved. Restart when ready.")
                  : jinaReaderDirty
                    ? tx("settings.status.restartAfterSaving", "Save changes, then restart when ready.")
                    : dirty
                      ? t("settings.byok.webSearch.saveHint")
                      : undefined
            }
            onSave={onSave}
            onRestart={onRestart}
            onReset={onReset}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

function AutomationsSettings({
  payload,
  loading,
  query,
  filter,
  sort,
  actionKey,
  error,
  onQueryChange,
  onFilterChange,
  onSortChange,
  onAction,
  onRequestEdit,
  onRequestDelete,
}: {
  payload: AutomationsPayload | null;
  loading: boolean;
  query: string;
  filter: AutomationFilter;
  sort: AutomationSort;
  actionKey: string | null;
  error: string | null;
  onQueryChange: (value: string) => void;
  onFilterChange: (value: AutomationFilter) => void;
  onSortChange: (value: AutomationSort) => void;
  onAction: (action: AutomationAction, job: SessionAutomationJob) => void | Promise<void>;
  onRequestEdit: (job: SessionAutomationJob) => void;
  onRequestDelete: (job: SessionAutomationJob) => void;
}) {
  const { t, i18n } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const jobs = payload?.jobs ?? [];
  const locale = i18n.resolvedLanguage || i18n.language;
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const filtered = useMemo(() => {
    const searchTokens = parseAutomationSearchQuery(query);
    return sortAutomationJobs(jobs, sort)
      .filter((job) => automationMatchesFilter(job, filter))
      .filter((job) => !searchTokens.length || automationMatchesSearch(job, searchTokens));
  }, [filter, jobs, query, sort]);
  const activeCount = jobs.filter((job) => {
    const key = automationStatusKey(job);
    return key === "active" || key === "running";
  }).length;
  const pausedCount = jobs.filter((job) => automationStatusKey(job) === "paused").length;
  const failedCount = jobs.filter(automationNeedsAttention).length;
  const systemCount = jobs.filter((job) => job.protected).length;
  const summaryOptions: Array<{ value: AutomationFilter; label: string; count: number }> = [
    { value: "all", label: tx("settings.automations.filters.all", "All"), count: jobs.length },
    { value: "active", label: tx("settings.automations.filters.active", "Active"), count: activeCount },
    { value: "paused", label: tx("settings.automations.filters.paused", "Paused"), count: pausedCount },
    { value: "failed", label: tx("settings.automations.filters.failed", "Needs attention"), count: failedCount },
    { value: "system", label: tx("settings.automations.filters.system", "System"), count: systemCount },
  ];
  const sortLabel = {
    next: tx("settings.automations.sort.next", "Next run"),
    last: tx("settings.automations.sort.last", "Last run"),
    updated: tx("settings.automations.sort.updated", "Updated"),
    name: tx("settings.automations.sort.name", "Name"),
  } satisfies Record<AutomationSort, string>;
  const selectedJob = filtered.find((job) => job.id === selectedJobId) ?? filtered[0] ?? null;

  useEffect(() => {
    if (!filtered.length) {
      if (selectedJobId !== null) setSelectedJobId(null);
      return;
    }
    if (!selectedJobId || !filtered.some((job) => job.id === selectedJobId)) {
      setSelectedJobId(filtered[0].id);
    }
  }, [filtered, selectedJobId]);

  return (
    <div className="space-y-5">
      <section className="shrink-0">
        <div className="mx-auto flex w-full max-w-[56rem] flex-col gap-3">
          <div className="-mx-1 overflow-x-auto px-1 pb-0.5">
            <div className="grid w-full min-w-[36rem] grid-cols-5 gap-1 rounded-[15px] bg-muted/42 p-1 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.035)] dark:bg-background/30">
              {summaryOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => onFilterChange(option.value)}
                  className={cn(
                    "inline-flex h-8 min-w-0 shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-[11px] px-3 text-[12px] font-medium text-muted-foreground transition-colors",
                    filter === option.value &&
                      "bg-background text-foreground shadow-[0_8px_20px_rgba(15,23,42,0.07)] dark:bg-background/80",
                    automationFilterToneClass(option.value, option.count, filter === option.value),
                  )}
                >
                  <span>{option.label}</span>
                  <span
                    className={cn(
                      "min-w-5 shrink-0 rounded-full bg-background/75 px-1.5 py-0.5 text-center text-[11px] tabular-nums text-muted-foreground",
                      automationFilterCountClass(option.value, option.count),
                    )}
                  >
                    {option.count}
                  </span>
                </button>
              ))}
            </div>
          </div>

          <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
            <div className="relative min-w-0">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/70" />
              <Input
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                placeholder={tx(
                  "settings.automations.search",
                  "Search task, message, linked chat, or schedule",
                )}
                className="h-9 w-full rounded-[13px] border-border/45 bg-background/85 pl-9 text-[13px] shadow-[0_8px_22px_rgba(15,23,42,0.04)] dark:border-white/10 dark:bg-background/40"
              />
            </div>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="inline-flex h-9 min-w-[8.5rem] items-center justify-center gap-1.5 whitespace-nowrap rounded-[13px] border border-border/45 bg-background/85 px-3 text-[12px] font-medium text-muted-foreground shadow-[0_8px_22px_rgba(15,23,42,0.04)] transition-colors hover:bg-muted/70 hover:text-foreground dark:border-white/10 dark:bg-background/40 sm:w-auto"
                >
                  <ArrowUpDown className="h-3.5 w-3.5" aria-hidden />
                  <span>{sortLabel[sort]}</span>
                  <ChevronDown className="h-3.5 w-3.5" aria-hidden />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-40">
                {(Object.keys(sortLabel) as AutomationSort[]).map((value) => (
                  <DropdownMenuItem key={value} onClick={() => onSortChange(value)}>
                    <span>{sortLabel[value]}</span>
                    {sort === value ? <Check className="ml-auto h-3.5 w-3.5" aria-hidden /> : null}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </section>

      {error ? (
        <div className="flex items-center gap-2 rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          <CircleAlert className="h-4 w-4 shrink-0" aria-hidden />
          <span>{error}</span>
        </div>
      ) : null}

      {loading && !payload ? (
        <div className="cyber-glass-panel relative overflow-hidden flex h-44 items-center justify-center rounded-[24px] text-[13px] text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
          {tx("settings.automations.loading", "Loading automations...")}
        </div>
      ) : filtered.length && selectedJob ? (
        <section className="grid min-h-0 overflow-hidden rounded-[22px] border border-border/45 bg-transparent shadow-none dark:border-white/10 xl:grid-cols-[minmax(16rem,18rem)_minmax(0,1fr)] xl:items-stretch">
          <aside className="flex min-h-0 flex-col overflow-hidden border-b border-border/35 bg-background/36 dark:border-white/10 dark:bg-background/18 xl:border-b-0 xl:border-r">
            <div className="flex shrink-0 items-center justify-between gap-3 px-4 py-3">
              <h2 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
                {tx("settings.automations.queue", "Queue")}
              </h2>
              <span className="rounded-full bg-orange-100/60 px-2 py-0.5 text-[11px] text-orange-800/70 tabular-nums dark:bg-orange-300/10 dark:text-orange-200/75">
                {filtered.length}
              </span>
            </div>
            <div
              className="max-h-[28rem] space-y-1 overflow-y-auto overscroll-contain px-2 pb-2 xl:max-h-[calc(100vh-24rem)]"
              role="list"
              aria-label={tx("settings.automations.queue", "Queue")}
            >
              {filtered.map((job) => (
                <AutomationListItem
                  key={job.id}
                  job={job}
                  locale={locale}
                  selected={job.id === selectedJob.id}
                  onSelect={() => setSelectedJobId(job.id)}
                />
              ))}
            </div>
          </aside>
          <AutomationDetailPanel
            job={selectedJob}
            locale={locale}
            actionKey={actionKey}
            onAction={onAction}
            onRequestEdit={onRequestEdit}
            onRequestDelete={onRequestDelete}
          />
        </section>
      ) : (
        <div className="cyber-glass-panel relative overflow-hidden rounded-[24px] px-5 py-12 text-center text-[13px] text-muted-foreground">
          <div>
            {jobs.length
              ? tx("settings.automations.noMatches", "No automations match this view.")
              : tx("settings.automations.empty", "No automations yet.")}
          </div>
          {!jobs.length ? (
            <div className="mx-auto mt-2 max-w-[28rem] text-[12px] leading-5">
              {tx(
                "settings.automations.emptyHint",
                "Create one from where it should run so biscuitbot keeps the right context.",
              )}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function AutomationListItem({
  job,
  locale,
  selected,
  onSelect,
}: {
  job: SessionAutomationJob;
  locale: string;
  selected: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const status = automationStatus(job, tx);
  const origin = automationOriginLabel(job, tx);
  const nextRun = formatAutomationNext(job, tx);

  return (
    <div role="listitem">
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        className={cn(
          "group grid w-full grid-cols-[minmax(0,1fr)_auto] gap-3 rounded-[18px] px-3 py-3.5 text-left transition-colors",
          selected
            ? "bg-background text-foreground shadow-[0_10px_28px_rgba(15,23,42,0.055)] ring-1 ring-border/45 dark:bg-background/45 dark:ring-white/10"
            : "text-muted-foreground hover:bg-white/48 hover:text-foreground dark:hover:bg-background/24",
        )}
      >
        <span className="min-w-0">
          <span className="flex min-w-0 items-center gap-2.5">
            <span
              className={cn("h-2 w-2 shrink-0 rounded-full", automationStatusDotClass(job))}
              aria-hidden
            />
            <span className="truncate text-[13.5px] font-medium text-foreground">
              {job.name || job.id}
            </span>
          </span>
          <span className="mt-1.5 line-clamp-2 text-[12px] leading-5 text-muted-foreground">
            {job.payload.message || tx("settings.automations.systemTask", "System-managed automation")}
          </span>
          <span className="mt-2.5 flex min-w-0 items-center gap-2 text-[11.5px] leading-none text-muted-foreground">
            <span className="truncate" title={formatAutomationNextTitle(job, locale, tx)}>
              {nextRun}
            </span>
            <span className="h-1 w-1 shrink-0 rounded-full bg-muted-foreground/35" aria-hidden />
            <span className="truncate">{origin}</span>
          </span>
        </span>
        <span className="flex shrink-0 flex-col items-end gap-2 pt-0.5">
          <span className="rounded-full bg-white/65 px-2 py-0.5 text-[11px] font-medium text-muted-foreground shadow-[inset_0_0_0_1px_rgba(120,72,25,0.055)] dark:bg-background/35">
            {status.label}
          </span>
          {job.delete_after_run ? (
            <span className="rounded-full bg-background/80 px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
              {tx("settings.automations.oneShot", "One-time")}
            </span>
          ) : null}
          <ChevronRight
            className={cn(
              "h-3.5 w-3.5 text-muted-foreground/55 transition-opacity",
              selected ? "opacity-100" : "opacity-0 group-hover:opacity-70",
            )}
            aria-hidden
          />
        </span>
      </button>
    </div>
  );
}

function AutomationDetailPanel({
  job,
  locale,
  actionKey,
  onAction,
  onRequestEdit,
  onRequestDelete,
}: {
  job: SessionAutomationJob;
  locale: string;
  actionKey: string | null;
  onAction: (action: AutomationAction, job: SessionAutomationJob) => void | Promise<void>;
  onRequestEdit: (job: SessionAutomationJob) => void;
  onRequestDelete: (job: SessionAutomationJob) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const status = automationStatus(job, tx);
  const origin = automationOriginLabel(job, tx);
  const originHref = job.origin?.channel === "websocket" && job.origin.session_key
    ? `#/chat/${encodeURIComponent(job.origin.session_key)}`
    : null;
  const created = job.created_at_ms ? fmtDateTime(job.created_at_ms, locale) : null;
  const updated = job.updated_at_ms ? fmtDateTime(job.updated_at_ms, locale) : null;
  const message = job.payload.message || tx("settings.automations.systemTask", "System-managed automation");
  const schedule = formatAutomationSchedule(job, locale, tx);
  const [messageExpanded, setMessageExpanded] = useState(false);
  const messageNeedsExpansion = automationMessageNeedsExpansion(message);

  useEffect(() => {
    setMessageExpanded(false);
  }, [job.id]);

  return (
    <article className="flex min-h-0 min-w-0 flex-col overflow-hidden bg-background/42 dark:bg-background/18">
      <div className="shrink-0 border-b border-border/35 px-4 py-3.5 dark:border-white/10 sm:px-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <h3 className="min-w-0 truncate text-[18px] font-medium leading-7 text-foreground">
                {job.name || job.id}
              </h3>
              <AutomationStatusBadge tone={status.tone}>{status.label}</AutomationStatusBadge>
              {job.delete_after_run ? (
                <AutomationStatusBadge>{tx("settings.automations.oneShot", "One-time")}</AutomationStatusBadge>
              ) : null}
            </div>
            <p className="mt-1 truncate text-[12.5px] leading-5 text-muted-foreground">
              {schedule} · {origin}
            </p>
          </div>
          <AutomationActionGroup
            job={job}
            actionKey={actionKey}
            onAction={onAction}
            onRequestEdit={onRequestEdit}
            onRequestDelete={onRequestDelete}
          />
        </div>
      </div>

      <div className="grid min-h-0 min-w-0 flex-1 overflow-hidden lg:grid-cols-[minmax(0,1fr)_14.5rem]">
        <div className="min-h-0 min-w-0 space-y-3 overflow-y-auto overscroll-contain p-4 sm:p-5">
          <section className="rounded-[20px] border border-border/35 bg-background/62 px-4 py-3.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.58)] dark:border-white/10 dark:bg-background/24">
            <div className="text-[11px] font-medium leading-none text-muted-foreground/75">
              {tx("settings.automations.fields.message", "Message")}
            </div>
            <div
              className={cn(
                "mt-3 whitespace-pre-wrap break-words text-[13px] leading-6 text-foreground/85",
                !messageExpanded && messageNeedsExpansion && "line-clamp-6",
              )}
            >
              {message}
            </div>
            {messageNeedsExpansion ? (
              <button
                type="button"
                className="mt-3 inline-flex text-[12px] font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                onClick={() => setMessageExpanded((value) => !value)}
              >
                {messageExpanded
                  ? tx("settings.automations.message.showLess", "Show less")
                  : tx("settings.automations.message.showMore", "Show full message")}
              </button>
            ) : null}
          </section>

          <div className="grid gap-3 md:grid-cols-2">
            <AutomationDetail
              label={tx("settings.automations.labels.next", "Next")}
              title={formatAutomationNextTitle(job, locale, tx)}
            >
              {formatAutomationNext(job, tx)}
            </AutomationDetail>
            <AutomationDetail label={tx("settings.automations.labels.origin", "Linked chat")} title={origin}>
              {originHref ? (
                <a
                  className="inline-flex max-w-full items-center gap-1 text-foreground/80 underline-offset-2 hover:underline"
                  href={originHref}
                >
                  <span className="truncate">{origin}</span>
                  <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
                </a>
              ) : (
                origin
              )}
            </AutomationDetail>
          </div>

          {job.state.last_error ? (
            <div className="rounded-[16px] border border-destructive/20 bg-destructive/8 px-3 py-2 text-[12px] leading-5 text-destructive">
              {job.state.last_error}
            </div>
          ) : null}
        </div>

        <aside className="min-h-0 overflow-y-auto overscroll-contain border-t border-border/35 bg-muted/20 p-4 text-[12px] text-muted-foreground dark:border-white/10 dark:bg-background/16 lg:border-l lg:border-t-0">
          <div className="grid gap-3">
            <AutomationDetail
              label={tx("settings.automations.labels.schedule", "Schedule")}
              title={schedule}
            >
              {schedule}
            </AutomationDetail>
            <div className="rounded-[18px] bg-background/55 p-3">
              <div className="grid gap-3">
                {created ? (
                  <div>
                    <div className="text-[11px] leading-none text-muted-foreground/75">
                      {tx("settings.automations.labels.created", "Created")}
                    </div>
                    <div className="mt-1.5 text-[12.5px] leading-5 text-foreground/80">{created}</div>
                  </div>
                ) : null}
                {updated ? (
                  <div>
                    <div className="text-[11px] leading-none text-muted-foreground/75">
                      {tx("settings.automations.labels.updated", "Updated")}
                    </div>
                    <div className="mt-1.5 text-[12.5px] leading-5 text-foreground/80">{updated}</div>
                  </div>
                ) : null}
                <div>
                  <div className="text-[11px] leading-none text-muted-foreground/75">ID</div>
                  <div className="mt-1.5 break-all font-mono text-[11.5px] leading-5 text-foreground/70">
                    {job.id}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </aside>
      </div>
    </article>
  );
}

function AutomationActionGroup({
  job,
  actionKey,
  onAction,
  onRequestEdit,
  onRequestDelete,
}: {
  job: SessionAutomationJob;
  actionKey: string | null;
  onAction: (action: AutomationAction, job: SessionAutomationJob) => void | Promise<void>;
  onRequestEdit: (job: SessionAutomationJob) => void;
  onRequestDelete: (job: SessionAutomationJob) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const canManage = !job.protected;
  const hasLinkedChat = Boolean(job.origin);
  const canRun = canManage && hasLinkedChat && job.enabled && !job.state.pending;
  const toggleAction: AutomationAction = job.enabled ? "disable" : "enable";
  const canToggle = canManage && (job.enabled || hasLinkedChat);
  const toggleBusy = actionKey === `${toggleAction}:${job.id}`;

  if (!canManage) {
    return (
      <span className="inline-flex h-9 items-center rounded-full bg-muted px-3 text-[12px] font-medium text-muted-foreground">
        {tx("settings.automations.protected", "Protected")}
      </span>
    );
  }

  return (
    <div className="flex shrink-0 items-center gap-1 rounded-full border border-border/35 bg-background/70 p-1 shadow-[0_10px_26px_rgba(15,23,42,0.055)] dark:border-white/10 dark:bg-background/35">
      <AppsActionButton
        ariaLabel={tx("settings.automations.edit", "Edit")}
        disabled={Boolean(actionKey)}
        onClick={() => onRequestEdit(job)}
      >
        <Pencil className="h-4 w-4" aria-hidden />
      </AppsActionButton>
      <AppsActionButton
        ariaLabel={tx("settings.automations.runNow", "Run now")}
        busy={actionKey === `run:${job.id}`}
        disabled={!canRun}
        onClick={() => void onAction("run", job)}
      >
        <PlayCircle className="h-4 w-4" aria-hidden />
      </AppsActionButton>
      <AppsActionButton
        ariaLabel={
          job.enabled
            ? tx("settings.automations.pause", "Pause")
            : tx("settings.automations.resume", "Resume")
        }
        busy={toggleBusy}
        disabled={!canToggle}
        onClick={() => void onAction(toggleAction, job)}
      >
        {job.enabled ? (
          <PauseCircle className="h-4 w-4" aria-hidden />
        ) : (
          <PlayCircle className="h-4 w-4" aria-hidden />
        )}
      </AppsActionButton>
      <AppsActionButton
        ariaLabel={tx("settings.automations.delete", "Delete")}
        tone="danger"
        disabled={Boolean(actionKey)}
        onClick={() => onRequestDelete(job)}
      >
        <Trash2 className="h-4 w-4" aria-hidden />
      </AppsActionButton>
    </div>
  );
}

function AutomationStatusBadge({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "success" | "warning";
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex h-6 items-center rounded-full px-2.5 text-[11.5px] font-medium shadow-[inset_0_0_0_1px_rgba(120,72,25,0.055)]",
        tone === "success" &&
          "bg-orange-100/72 text-orange-800 dark:bg-orange-300/12 dark:text-orange-200",
        tone === "warning" &&
          "bg-amber-100/80 text-amber-800 dark:bg-amber-300/14 dark:text-amber-200",
        tone === "neutral" &&
          "bg-white/64 text-muted-foreground dark:bg-background/35 dark:text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function automationMessageNeedsExpansion(message: string): boolean {
  return message.length > 360 || message.split(/\r?\n/).length > 6;
}

function AutomationDetail({
  label,
  title,
  secondary,
  children,
}: {
  label: string;
  title?: string;
  secondary?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0 rounded-[17px] bg-background/52 px-3 py-3 shadow-[inset_0_0_0_1px_rgba(15,23,42,0.035)] dark:bg-background/22">
      <div className="text-[11px] font-medium leading-none text-muted-foreground/75">
        {label}
      </div>
      <div className="mt-1.5 min-w-0">
        <div className="line-clamp-2 text-[13px] leading-5 text-foreground/85" title={title}>
          {children}
        </div>
        {secondary ? (
          <div className="mt-0.5 truncate text-[11.5px] leading-4 text-muted-foreground" title={title}>
            {secondary}
          </div>
        ) : null}
      </div>
    </div>
  );
}

type AutomationEveryUnit = "second" | "minute" | "hour" | "day";

type AutomationEditDraft = {
  name: string;
  message: string;
  scheduleKind: "at" | "every" | "cron";
  everyValue: string;
  everyUnit: AutomationEveryUnit;
  cronExpr: string;
  tz: string;
  atLocal: string;
};
type AutomationScheduleUpdate = NonNullable<AutomationUpdatePayload["schedule"]>;

const AUTOMATION_EVERY_UNITS: Array<{ value: AutomationEveryUnit; ms: number }> = [
  { value: "second", ms: 1000 },
  { value: "minute", ms: 60_000 },
  { value: "hour", ms: 3_600_000 },
  { value: "day", ms: 86_400_000 },
];

function AutomationEditDialog({
  job,
  saving,
  onOpenChange,
  onSave,
}: {
  job: SessionAutomationJob | null;
  saving: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (job: SessionAutomationJob, values: AutomationUpdatePayload) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const [draft, setDraft] = useState<AutomationEditDraft>(() => automationDraftFromJob(null));

  useEffect(() => {
    setDraft(automationDraftFromJob(job));
  }, [job]);

  const validation = automationEditDraftError(draft, job, tx);
  const scheduleOptions = [
    { value: "every", label: tx("settings.automations.scheduleTypes.every", "Interval") },
    { value: "cron", label: tx("settings.automations.scheduleTypes.cron", "Cron") },
    { value: "at", label: tx("settings.automations.scheduleTypes.at", "Once") },
  ];
  const unitLabels: Record<AutomationEveryUnit, string> = {
    second: tx("settings.automations.everyUnits.second", "Seconds"),
    minute: tx("settings.automations.everyUnits.minute", "Minutes"),
    hour: tx("settings.automations.everyUnits.hour", "Hours"),
    day: tx("settings.automations.everyUnits.day", "Days"),
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const payload = automationUpdatePayloadFromDraft(draft, job);
    if (!job || typeof payload === "string") return;
    void onSave(job, payload);
  };

  return (
    <Dialog open={Boolean(job)} onOpenChange={onOpenChange}>
      {job ? (
        <DialogContent
          aria-describedby={undefined}
          className="w-[min(calc(100vw-2rem),34rem)] rounded-[26px]"
        >
          <form className="space-y-5" onSubmit={submit}>
            <DialogHeader>
              <DialogTitle>{tx("settings.automations.editTitle", "Edit automation")}</DialogTitle>
            </DialogHeader>

            <div className="space-y-4">
              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("settings.automations.fields.name", "Name")}
                </span>
                <Input
                  value={draft.name}
                  onChange={(event) => setDraft((prev) => ({ ...prev, name: event.target.value }))}
                  className="h-10 rounded-[12px]"
                />
              </label>

              <label className="block space-y-1.5">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("settings.automations.fields.message", "Message")}
                </span>
                <Textarea
                  value={draft.message}
                  onChange={(event) => setDraft((prev) => ({ ...prev, message: event.target.value }))}
                  className="min-h-[160px] resize-none rounded-[12px] text-[13px] leading-5"
                />
              </label>

              <div className="space-y-2">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("settings.automations.fields.scheduleType", "Schedule type")}
                </span>
                <SegmentedControl
                  value={draft.scheduleKind}
                  options={scheduleOptions}
                  onChange={(value) =>
                    setDraft((prev) => ({
                      ...prev,
                      scheduleKind: value as AutomationEditDraft["scheduleKind"],
                    }))
                  }
                />
              </div>

              {draft.scheduleKind === "every" ? (
                <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_10rem]">
                  <label className="block space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">
                      {tx("settings.automations.fields.every", "Every")}
                    </span>
                    <Input
                      type="number"
                      min={1}
                      step={1}
                      value={draft.everyValue}
                      onChange={(event) =>
                        setDraft((prev) => ({ ...prev, everyValue: event.target.value }))
                      }
                      className="h-10 rounded-[12px]"
                    />
                  </label>
                  <label className="block space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">
                      {tx("settings.automations.fields.unit", "Unit")}
                    </span>
                    <select
                      value={draft.everyUnit}
                      onChange={(event) =>
                        setDraft((prev) => ({
                          ...prev,
                          everyUnit: event.target.value as AutomationEveryUnit,
                        }))
                      }
                      className="h-10 w-full rounded-[12px] border border-input bg-background px-3 text-[13px] text-foreground shadow-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {AUTOMATION_EVERY_UNITS.map((unit) => (
                        <option key={unit.value} value={unit.value}>
                          {unitLabels[unit.value]}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              ) : null}

              {draft.scheduleKind === "cron" ? (
                <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_12rem]">
                  <label className="block space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">
                      {tx("settings.automations.fields.cronExpression", "Cron expression")}
                    </span>
                    <Input
                      value={draft.cronExpr}
                      onChange={(event) => setDraft((prev) => ({ ...prev, cronExpr: event.target.value }))}
                      placeholder="0 9 * * *"
                      className="h-10 rounded-[12px] font-mono text-[13px]"
                    />
                  </label>
                  <label className="block space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">
                      {tx("settings.automations.fields.timezone", "Timezone")}
                    </span>
                    <Input
                      value={draft.tz}
                      onChange={(event) => setDraft((prev) => ({ ...prev, tz: event.target.value }))}
                      placeholder="Asia/Shanghai"
                      className="h-10 rounded-[12px] text-[13px]"
                    />
                  </label>
                </div>
              ) : null}

              {draft.scheduleKind === "at" ? (
                <label className="block space-y-1.5">
                  <span className="text-[12px] font-medium text-muted-foreground">
                    {tx("settings.automations.fields.runAt", "Run at")}
                  </span>
                  <Input
                    type="datetime-local"
                    value={draft.atLocal}
                    onChange={(event) => setDraft((prev) => ({ ...prev, atLocal: event.target.value }))}
                    className="h-10 rounded-[12px]"
                  />
                </label>
              ) : null}

              {validation ? (
                <div className="rounded-[12px] bg-destructive/8 px-3 py-2 text-[12px] text-destructive">
                  {validation}
                </div>
              ) : null}
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                onClick={() => onOpenChange(false)}
                disabled={saving}
                className="rounded-full"
              >
                {tx("settings.automations.cancel", "Cancel")}
              </Button>
              <Button type="submit" disabled={Boolean(validation) || saving} className="rounded-full">
                {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
                {tx("settings.automations.save", "Save")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}

function AutomationDeleteDialog({
  job,
  deleting,
  onOpenChange,
  onConfirm,
}: {
  job: SessionAutomationJob | null;
  deleting: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (job: SessionAutomationJob) => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  return (
    <Dialog open={Boolean(job)} onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(calc(100vw-2rem),26rem)] rounded-[26px]">
        <DialogHeader>
          <DialogTitle>{tx("settings.automations.deleteTitle", "Delete automation")}</DialogTitle>
          <DialogDescription>
            {tx(
              "settings.automations.deleteDescription",
              "This removes {{name}} from the cron store. Past chat messages stay in the session.",
              { name: job?.name || job?.id || "" },
            )}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={deleting}
            className="rounded-full"
          >
            {tx("settings.automations.cancel", "Cancel")}
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={() => job && void onConfirm(job)}
            disabled={!job || deleting}
            className="rounded-full"
          >
            {deleting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
            {tx("settings.automations.delete", "Delete")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function automationNeedsAttention(job: SessionAutomationJob): boolean {
  return job.state.last_status === "error";
}

function automationStatusKey(
  job: SessionAutomationJob,
): "active" | "running" | "paused" | "failed" | "system" | "completed" | "idle" {
  if (job.protected) return "system";
  if (job.state.pending) return "running";
  if (!job.enabled) return "paused";
  if (job.state.last_status === "error") return "failed";
  if (job.delete_after_run && !job.state.next_run_at_ms && job.state.last_status === "ok") {
    return "completed";
  }
  if (!job.state.next_run_at_ms) return "idle";
  return "active";
}

function sortAutomationJobs(jobs: SessionAutomationJob[], sort: AutomationSort): SessionAutomationJob[] {
  const byName = (left: SessionAutomationJob, right: SessionAutomationJob) =>
    (left.name || left.id).localeCompare(right.name || right.id);
  return [...jobs].sort((left, right) => {
    if (sort === "name") return byName(left, right);
    if (sort === "last") {
      return (right.state.last_run_at_ms ?? 0) - (left.state.last_run_at_ms ?? 0) || byName(left, right);
    }
    if (sort === "updated") {
      return (right.updated_at_ms ?? 0) - (left.updated_at_ms ?? 0) || byName(left, right);
    }
    const leftNext = left.state.next_run_at_ms ?? Number.MAX_SAFE_INTEGER;
    const rightNext = right.state.next_run_at_ms ?? Number.MAX_SAFE_INTEGER;
    return leftNext - rightNext || byName(left, right);
  });
}

function automationDraftFromJob(job: SessionAutomationJob | null): AutomationEditDraft {
  const every = automationIntervalDraft(job?.schedule.every_ms ?? 3_600_000);
  const scheduleKind = job?.schedule.kind === "at" || job?.schedule.kind === "cron"
    ? job.schedule.kind
    : "every";
  return {
    name: job?.name ?? "",
    message: job?.payload.message ?? "",
    scheduleKind,
    everyValue: every.value,
    everyUnit: every.unit,
    cronExpr: job?.schedule.expr ?? "0 9 * * *",
    tz: job?.schedule.tz ?? "",
    atLocal: formatLocalDateTimeInput(job?.schedule.at_ms ?? Date.now() + 3_600_000),
  };
}

function automationIntervalDraft(ms: number): { value: string; unit: AutomationEveryUnit } {
  for (const unit of [...AUTOMATION_EVERY_UNITS].reverse()) {
    if (ms >= unit.ms && ms % unit.ms === 0) {
      return { value: String(ms / unit.ms), unit: unit.value };
    }
  }
  return { value: String(Math.max(1, Math.round(ms / 60_000))), unit: "minute" };
}

function formatLocalDateTimeInput(ms: number): string {
  const date = new Date(ms);
  if (!Number.isFinite(date.getTime())) return "";
  const local = new Date(ms - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function automationEditDraftError(
  draft: AutomationEditDraft,
  job: SessionAutomationJob | null,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string | null {
  if (!draft.name.trim()) return tx("settings.automations.validation.nameRequired", "Name is required.");
  if (!draft.message.trim()) {
    return tx("settings.automations.validation.messageRequired", "Message is required.");
  }
  if (draft.scheduleKind === "every") {
    const value = Number(draft.everyValue);
    if (!Number.isInteger(value) || value <= 0) {
      return tx("settings.automations.validation.intervalRequired", "Interval must be a positive number.");
    }
  }
  if (draft.scheduleKind === "cron" && !draft.cronExpr.trim()) {
    return tx("settings.automations.validation.cronRequired", "Cron expression is required.");
  }
  if (draft.scheduleKind === "at") {
    const atMs = new Date(draft.atLocal).getTime();
    if (!Number.isFinite(atMs)) {
      return tx("settings.automations.validation.timeRequired", "Run time is required.");
    }
    if (atMs <= Date.now() && automationScheduleChanged(draft, job)) {
      return tx("settings.automations.validation.futureRequired", "Run time must be in the future.");
    }
  }
  return null;
}

function automationUpdatePayloadFromDraft(
  draft: AutomationEditDraft,
  job: SessionAutomationJob | null,
): AutomationUpdatePayload | string {
  const name = draft.name.trim();
  const message = draft.message.trim();
  if (!name || !message) return "invalid";
  const payload: AutomationUpdatePayload = { name, message };
  const schedule = automationSchedulePayloadFromDraft(draft);
  if (typeof schedule === "string") return schedule;
  if (automationScheduleChanged(draft, job, schedule)) {
    payload.schedule = schedule;
  }
  return payload;
}

function automationSchedulePayloadFromDraft(draft: AutomationEditDraft): AutomationScheduleUpdate | string {
  if (draft.scheduleKind === "every") {
    const unit = AUTOMATION_EVERY_UNITS.find((candidate) => candidate.value === draft.everyUnit);
    const value = Number(draft.everyValue);
    if (!unit || !Number.isInteger(value) || value <= 0) return "invalid";
    return { kind: "every", every_ms: value * unit.ms };
  } else if (draft.scheduleKind === "cron") {
    const expr = draft.cronExpr.trim();
    if (!expr) return "invalid";
    return { kind: "cron", expr, ...(draft.tz.trim() ? { tz: draft.tz.trim() } : {}) };
  } else {
    const atMs = new Date(draft.atLocal).getTime();
    if (!Number.isFinite(atMs)) return "invalid";
    return { kind: "at", at_ms: atMs };
  }
}

function automationScheduleChanged(
  draft: AutomationEditDraft,
  job: SessionAutomationJob | null,
  schedule: AutomationScheduleUpdate | string = automationSchedulePayloadFromDraft(draft),
): boolean {
  if (!job || typeof schedule === "string") return true;
  if (schedule.kind !== job.schedule.kind) return true;
  if (schedule.kind === "every") return schedule.every_ms !== job.schedule.every_ms;
  if (schedule.kind === "cron") {
    return schedule.expr !== (job.schedule.expr ?? "") || (schedule.tz ?? null) !== (job.schedule.tz ?? null);
  }
  return draft.atLocal !== formatLocalDateTimeInput(job.schedule.at_ms ?? NaN);
}

type AutomationSearchField = "id" | "name" | "message" | "chat" | "cron" | "schedule" | "status";

interface AutomationSearchToken {
  field: AutomationSearchField | null;
  value: string;
}

const AUTOMATION_SEARCH_FIELDS = new Set<AutomationSearchField>([
  "id",
  "name",
  "message",
  "chat",
  "cron",
  "schedule",
  "status",
]);

const AUTOMATION_CHANNEL_LABELS: Record<string, string> = {
  api: "API",
  cli: "CLI",
  dingtalk: "DingTalk",
  email: "Email",
  feishu: "Feishu",
  qq: "QQ",
  wechat: "WeChat",
  wecom: "WeCom",
  weixin: "WeChat",
};

function parseAutomationSearchQuery(query: string): AutomationSearchToken[] {
  return (query.match(/[^\s:]+:"[^"]+"|"[^"]+"|\S+/g) ?? [])
    .map((rawPart): AutomationSearchToken | null => {
      const part = trimAutomationSearchValue(rawPart);
      if (!part) return null;
      const fieldMatch = part.match(/^([A-Za-z]+):(.*)$/);
      if (!fieldMatch) return { field: null, value: part.toLowerCase() };
      const field = fieldMatch[1].toLowerCase() as AutomationSearchField;
      const value = trimAutomationSearchValue(fieldMatch[2]).toLowerCase();
      if (!value) return null;
      return AUTOMATION_SEARCH_FIELDS.has(field)
        ? { field, value }
        : { field: null, value: part.toLowerCase() };
    })
    .filter((token): token is AutomationSearchToken => Boolean(token));
}

function trimAutomationSearchValue(value: string): string {
  return value.trim().replace(/^"|"$/g, "").trim();
}

function automationMatchesSearch(job: SessionAutomationJob, tokens: AutomationSearchToken[]): boolean {
  return tokens.every((token) => automationSearchText(job, token.field).includes(token.value));
}

function automationSearchText(job: SessionAutomationJob, field: AutomationSearchField | null = null): string {
  return automationSearchParts(job, field)
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function automationSearchParts(
  job: SessionAutomationJob,
  field: AutomationSearchField | null,
): Array<string | number | null | undefined> {
  const originParts = automationOriginSearchParts(job);
  const scheduleParts = automationScheduleSearchParts(job);
  if (field === "id") return [job.id];
  if (field === "name") return [job.name, job.id];
  if (field === "message") return [job.payload.message];
  if (field === "chat") return originParts;
  if (field === "cron" || field === "schedule") return scheduleParts;
  if (field === "status") return [automationStatusKey(job), job.enabled ? "enabled" : "disabled"];
  return [
    job.id,
    job.name,
    job.payload.message,
    ...scheduleParts,
    automationStatusKey(job),
    ...originParts,
  ];
}

function automationOriginSearchParts(job: SessionAutomationJob): Array<string | null | undefined> {
  const origin = job.origin;
  if (!origin) return [];
  const channel = origin.channel.trim().toLowerCase();
  return [
    origin.session_key,
    origin.title,
    origin.preview,
    origin.channel,
    AUTOMATION_CHANNEL_LABELS[channel],
  ];
}

function automationScheduleSearchParts(job: SessionAutomationJob): Array<string | number | null | undefined> {
  const schedule = job.schedule;
  const parts: Array<string | number | null | undefined> = [
    schedule.kind,
    schedule.expr,
    schedule.tz,
    schedule.every_ms,
    schedule.at_ms,
  ];
  if (schedule.kind === "cron" && schedule.expr) {
    parts.push(...automationCronSearchParts(schedule.expr));
  }
  return parts;
}

function automationCronSearchParts(expr: string): string[] {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return [];
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
  const everyDay = dayOfMonth === "*" && month === "*" && dayOfWeek === "*";
  const numericMinute = cronNumericToken(minute, 59);
  const numericHour = cronNumericToken(hour, 23);
  if (numericMinute === null) return [];
  const paddedMinute = String(numericMinute).padStart(2, "0");

  if (numericHour !== null) {
    const time = `${String(numericHour).padStart(2, "0")}:${paddedMinute}`;
    return [time, `:${paddedMinute}`];
  }

  if (everyDay && hour === "*") {
    return [`:${paddedMinute}`, `hourly at :${paddedMinute}`];
  }

  const range = /^(\d{1,2})-(\d{1,2})$/.exec(hour);
  if (!everyDay || !range) return [];
  const start = Number(range[1]);
  const end = Number(range[2]);
  if (start > 23 || end > 23) return [];
  const paddedRange = `${String(start).padStart(2, "0")}-${String(end).padStart(2, "0")}`;
  const rawRange = `${start}-${end}`;
  return [
    paddedRange,
    rawRange,
    `:${paddedMinute}`,
    `${paddedRange} at :${paddedMinute}`,
    `hourly ${paddedRange} at :${paddedMinute}`,
  ];
}

function automationMatchesFilter(job: SessionAutomationJob, filter: AutomationFilter): boolean {
  const status = automationStatusKey(job);
  if (filter === "active") return status === "active" || status === "running";
  if (filter === "paused") return status === "paused";
  if (filter === "failed") return automationNeedsAttention(job);
  if (filter === "system") return Boolean(job.protected);
  return true;
}

const AUTOMATION_FILTER_TONES: Partial<
  Record<AutomationFilter, { text: string; selectedText: string; count: string }>
> = {
  active: {
    text: "text-emerald-600 dark:text-emerald-400",
    selectedText: "text-emerald-700 dark:text-emerald-300",
    count: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  },
  paused: {
    text: "text-amber-600 dark:text-amber-400",
    selectedText: "text-amber-700 dark:text-amber-300",
    count: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  },
  failed: {
    text: "text-rose-600 dark:text-rose-400",
    selectedText: "text-rose-700 dark:text-rose-300",
    count: "bg-rose-500/10 text-rose-700 dark:text-rose-300",
  },
  system: {
    text: "text-sky-600 dark:text-sky-400",
    selectedText: "text-sky-700 dark:text-sky-300",
    count: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  },
};

function automationFilterToneClass(value: AutomationFilter, count: number, selected: boolean): string {
  const tone = AUTOMATION_FILTER_TONES[value];
  if (count <= 0 || !tone) return "";
  return selected ? tone.selectedText : tone.text;
}

function automationFilterCountClass(value: AutomationFilter, count: number): string {
  const tone = AUTOMATION_FILTER_TONES[value];
  return count > 0 && tone ? tone.count : "";
}

function automationStatus(
  job: SessionAutomationJob,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): { label: string; tone: "neutral" | "success" | "warning" } {
  const status = automationStatusKey(job);
  if (status === "system") return { label: tx("settings.automations.status.system", "System"), tone: "neutral" };
  if (status === "running") {
    return { label: tx("settings.automations.status.running", "Running now"), tone: "warning" };
  }
  if (status === "paused") return { label: tx("settings.automations.status.paused", "Paused"), tone: "neutral" };
  if (status === "failed") {
    return { label: tx("settings.automations.status.failed", "Failed"), tone: "warning" };
  }
  if (status === "completed") {
    return { label: tx("settings.automations.status.completed", "Completed"), tone: "neutral" };
  }
  if (status === "idle") {
    return { label: tx("settings.automations.status.noSchedule", "No schedule"), tone: "neutral" };
  }
  return { label: tx("settings.automations.status.active", "Active"), tone: "success" };
}

function automationOriginLabel(
  job: SessionAutomationJob,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string {
  if (job.protected) return tx("settings.automations.origin.system", "System");
  const origin = job.origin;
  if (!origin) return tx("settings.automations.origin.unknown", "No linked chat");
  if (origin.channel !== "websocket") return automationChannelLabel(origin.channel, tx);
  return origin.title || origin.preview || origin.session_key || automationChannelLabel(origin.channel, tx);
}

function automationChannelLabel(
  channel: string,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string {
  const key = channel.trim().toLowerCase();
  return AUTOMATION_CHANNEL_LABELS[key]
    ? tx(`settings.automations.channels.${key}`, AUTOMATION_CHANNEL_LABELS[key])
    : channel;
}

function formatAutomationSchedule(
  job: SessionAutomationJob,
  locale: string,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string {
  if (job.schedule.kind === "at" && job.schedule.at_ms) {
    return tx("settings.automations.schedule.at", "At {{time}}", {
      time: fmtDateTime(job.schedule.at_ms, locale),
    });
  }
  if (job.schedule.kind === "every" && job.schedule.every_ms) {
    return tx("settings.automations.schedule.every", "Every {{duration}}", {
      duration: formatAutomationInterval(job.schedule.every_ms, locale),
    });
  }
  if (job.schedule.kind === "cron" && job.schedule.expr) {
    const summary = formatCronScheduleSummary(job.schedule.expr, tx);
    if (summary) {
      return job.schedule.tz
        ? tx("settings.automations.schedule.withTz", "{{summary}} · {{tz}}", {
            summary,
            tz: job.schedule.tz,
          })
        : summary;
    }
    return job.schedule.tz
      ? tx("settings.automations.schedule.cronWithTz", "Cron {{expr}} · {{tz}}", {
          expr: job.schedule.expr,
          tz: job.schedule.tz,
        })
      : tx("settings.automations.schedule.cron", "Cron {{expr}}", { expr: job.schedule.expr });
  }
  return tx("settings.automations.schedule.custom", "Custom schedule");
}

function formatCronScheduleSummary(
  expr: string,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
  const numericMinute = cronNumericToken(minute, 59);
  const numericHour = cronNumericToken(hour, 23);
  const everyDay = dayOfMonth === "*" && month === "*" && dayOfWeek === "*";
  const workdays = dayOfMonth === "*" && month === "*" && ["1-5", "MON-FRI", "mon-fri"].includes(dayOfWeek);

  if (numericMinute !== null && numericHour !== null) {
    const time = `${String(numericHour).padStart(2, "0")}:${String(numericMinute).padStart(2, "0")}`;
    if (everyDay) return tx("settings.automations.schedule.dailyAt", "Daily at {{time}}", { time });
    if (workdays) return tx("settings.automations.schedule.weekdaysAt", "Weekdays at {{time}}", { time });
  }

  if (everyDay && numericMinute !== null && hour === "*") {
    return tx("settings.automations.schedule.hourlyAt", "Hourly at :{{minute}}", {
      minute: String(numericMinute).padStart(2, "0"),
    });
  }

  const range = /^(\d{1,2})-(\d{1,2})$/.exec(hour);
  if (everyDay && numericMinute !== null && range) {
    const start = Number(range[1]);
    const end = Number(range[2]);
    if (start > 23 || end > 23) return null;
    return tx("settings.automations.schedule.hourlyWindow", "Hourly {{start}}-{{end}} at :{{minute}}", {
      start: String(start).padStart(2, "0"),
      end: String(end).padStart(2, "0"),
      minute: String(numericMinute).padStart(2, "0"),
    });
  }

  return null;
}

function cronNumericToken(value: string, max: number): number | null {
  if (!/^\d{1,2}$/.test(value)) return null;
  const parsed = Number(value);
  return parsed <= max ? parsed : null;
}

function formatAutomationNext(
  job: SessionAutomationJob,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string {
  if (!job.enabled) return tx("settings.automations.next.paused", "Paused");
  if (job.state.pending) return tx("settings.automations.next.pending", "Running now");
  if (!job.state.next_run_at_ms) return tx("settings.automations.next.none", "No next run");
  return relativeTime(job.state.next_run_at_ms);
}

function formatAutomationNextTitle(
  job: SessionAutomationJob,
  locale: string,
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string,
): string {
  if (!job.state.next_run_at_ms) return formatAutomationNext(job, tx);
  return fmtDateTime(job.state.next_run_at_ms, locale);
}

function automationStatusDotClass(job: SessionAutomationJob): string {
  const status = automationStatusKey(job);
  if (status === "active" || status === "running") return "bg-orange-500 shadow-[0_0_0_3px_rgba(249,115,22,0.12)]";
  if (status === "failed") return "bg-amber-500 shadow-[0_0_0_3px_rgba(245,158,11,0.13)]";
  if (status === "system") return "bg-muted-foreground/45";
  return "bg-muted-foreground/45";
}

function formatAutomationUnit(
  value: number,
  unit: Intl.NumberFormatOptions["unit"],
  locale: string,
  maximumFractionDigits = 0,
): string {
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit,
    unitDisplay: "long",
    maximumFractionDigits,
  }).format(value);
}

function formatAutomationInterval(ms: number, locale: string): string {
  const units: Array<[Intl.NumberFormatOptions["unit"], number]> = [
    ["day", 86_400_000],
    ["hour", 3_600_000],
    ["minute", 60_000],
    ["second", 1000],
  ];
  for (const [unit, size] of units) {
    if (ms >= size && ms % size === 0) return formatAutomationUnit(ms / size, unit, locale);
  }
  const fallbackUnit = ms < 60_000 ? "second" : "minute";
  const fallbackSize = fallbackUnit === "second" ? 1000 : 60_000;
  return formatAutomationUnit(ms / fallbackSize, fallbackUnit, locale, 1);
}

const AppsActionButton = forwardRef<HTMLButtonElement, {
  ariaLabel: string;
  busy?: boolean;
  disabled?: boolean;
  tone?: "default" | "installed" | "danger";
  onClick?: () => void;
  children: ReactNode;
}>(function AppsActionButton({
  ariaLabel,
  busy,
  disabled,
  tone = "default",
  onClick,
  children,
}, ref) {
  return (
    <Button
      ref={ref}
      type="button"
      size="icon"
      variant="ghost"
      aria-label={ariaLabel}
      title={ariaLabel}
      disabled={disabled || busy}
      onClick={onClick}
      className={cn(
        "h-9 w-9 rounded-full text-muted-foreground transition-colors",
        tone === "installed" && "bg-transparent hover:bg-muted/70 hover:text-foreground",
        tone === "danger" && "bg-transparent hover:bg-destructive/10 hover:text-destructive",
        tone === "default" && "bg-muted/70 hover:bg-muted hover:text-foreground",
      )}
    >
      {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : children}
    </Button>
  );
});

function RuntimeSettings({
  form,
  setForm,
  settings,
  dirty,
  saving,
  onSave,
  onRestart,
  isRestarting,
  requiresRestartPending,
}: {
  form: AgentSettingsDraft;
  setForm: Dispatch<SetStateAction<AgentSettingsDraft>>;
  settings: SettingsPayload;
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
  requiresRestartPending: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const isNativeHost = getHostApi() !== null || (settings.surface ?? settings.runtime_surface) === "native";
  const restartActionLabel = isNativeHost
    ? tx("app.system.restartEngine", "Restart engine")
    : t("app.system.restart");
  const restartingActionLabel = isNativeHost
    ? tx("app.system.restartingEngine", "Restarting engine...")
    : t("app.system.restarting");
  const [diagnosticsPath, setDiagnosticsPath] = useState<string | null>(null);
  const [hostActionMessage, setHostActionMessage] = useState<{
    target: "logs" | "diagnostics";
    message: string;
  } | null>(null);
  const [hostActionBusy, setHostActionBusy] =
    useState<"logs" | "diagnostics" | null>(null);
  const hostApi = getHostApi();
  const engineState = isRestarting
    ? tx("settings.values.restartingEngine", "Restarting")
    : settings.apply_state?.status === "pending"
      ? tx("settings.values.pending", "Pending")
      : tx("settings.values.ready", "Ready");
  const runHostAction = async (
    target: "logs" | "diagnostics",
    action: () => Promise<string | void>,
    successMessage: (result: string | void) => string,
    failureMessage: string,
  ) => {
    if (!hostApi) {
      setHostActionMessage({
        target,
        message: tx(
          "settings.status.hostApiUnavailable",
          "Host actions are only available inside the native app.",
        ),
      });
      return;
    }
    setHostActionBusy(target);
    setHostActionMessage(null);
    try {
      const result = await action();
      setHostActionMessage({ target, message: successMessage(result) });
    } catch {
      setHostActionMessage({ target, message: failureMessage });
    } finally {
      setHostActionBusy(null);
    }
  };
  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>{tx("settings.sections.identity", "Identity")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow title={tx("settings.rows.botName", "Bot name")} description={tx("settings.help.botName", "Shown wherever biscuitbot uses a display name.")}>
            <Input
              value={form.botName}
              onChange={(event) => setForm((prev) => ({ ...prev, botName: event.target.value }))}
              className="h-8 w-[220px] rounded-full text-[13px]"
            />
          </SettingsRow>
          <SettingsRow title={tx("settings.rows.botIcon", "Bot icon")} description={tx("settings.help.botIcon", "Short emoji or text shown with the bot name.")}>
            <Input
              value={form.botIcon}
              onChange={(event) => setForm((prev) => ({ ...prev, botIcon: event.target.value }))}
              className="h-8 w-[120px] rounded-full text-center text-[13px]"
            />
          </SettingsRow>
          <SettingsRow title={tx("settings.rows.timezone", "Timezone")} description={tx("settings.help.timezone", "Used for schedules and time-aware replies.")}>
            <TimezonePicker
              value={form.timezone}
              onChange={(timezone) => setForm((prev) => ({ ...prev, timezone }))}
            />
          </SettingsRow>
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            dirtyMessage={
              isNativeHost
                ? tx("settings.status.hostRestartAfterSaving", "Save changes and biscuitbot will restart its engine.")
                : tx("settings.status.restartAfterSaving", "Save changes, then restart when ready.")
            }
            pendingMessage={
              isNativeHost
                ? tx("settings.status.hostRestartPending", "Saved. Restarting engine when ready.")
                : tx("settings.status.savedRestartApply", "Saved. Restart when ready.")
            }
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>

      {isNativeHost ? (
        <section>
          <SettingsSectionTitle>{tx("settings.sections.nativeHost", "Native host")}</SettingsSectionTitle>
          <SettingsGroup>
            <ReadOnlyRow title={tx("settings.rows.engine", "Engine")} value={engineState} />
            {settings.runtime_capabilities?.can_open_logs ? (
              <SettingsRow
                title={tx("settings.rows.logs", "Logs")}
                description={
                  hostActionMessage?.target === "logs"
                    ? hostActionMessage.message
                    : tx("settings.help.logs", "Open the native engine log folder.")
                }
              >
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    void runHostAction(
                      "logs",
                      () => hostApi!.openLogs(),
                      () => tx("settings.status.logsOpened", "Opened logs folder."),
                      tx("settings.status.logsOpenFailed", "Could not open logs folder."),
                    )
                  }
                  disabled={hostActionBusy !== null}
                  className="rounded-full"
                >
                  {hostActionBusy === "logs"
                    ? tx("settings.actions.opening", "Opening...")
                    : tx("settings.actions.open", "Open")}
                </Button>
              </SettingsRow>
            ) : null}
            {settings.runtime_capabilities?.can_export_diagnostics ? (
              <SettingsRow
                title={tx("settings.rows.diagnostics", "Diagnostics")}
                description={
                  hostActionMessage?.target === "diagnostics"
                    ? hostActionMessage.message
                    : diagnosticsPath
                    ? diagnosticsPath
                    : tx("settings.help.diagnostics", "Export a small runtime report for support.")
                }
              >
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    void runHostAction(
                      "diagnostics",
                      async () => {
                        const path = await hostApi!.exportDiagnostics();
                        setDiagnosticsPath(path);
                        return path;
                      },
                      (path) =>
                        t("settings.status.diagnosticsExported", {
                          path: String(path ?? ""),
                          defaultValue: "Diagnostics exported to {{path}}.",
                        }),
                      tx("settings.status.diagnosticsExportFailed", "Could not export diagnostics."),
                    )
                  }
                  disabled={hostActionBusy !== null}
                  className="rounded-full"
                >
                  {hostActionBusy === "diagnostics"
                    ? tx("settings.actions.exporting", "Exporting...")
                    : tx("settings.actions.export", "Export")}
                </Button>
              </SettingsRow>
            ) : null}
          </SettingsGroup>
        </section>
      ) : null}

      <section>
        <SettingsSectionTitle>{t("settings.sections.system")}</SettingsSectionTitle>
        <SettingsGroup>
          {!isNativeHost ? (
            <ReadOnlyRow
              title={tx("settings.rows.gateway", "Gateway")}
              value={`${settings.runtime.gateway_host}:${settings.runtime.gateway_port}`}
            />
          ) : null}
          <ReadOnlyRow title={t("settings.rows.configPath")} value={settings.runtime.config_path} />
          <ReadOnlyRow title={tx("settings.rows.workspacePath", "Default workspace")} value={settings.runtime.workspace_path} />
          {onRestart && !requiresRestartPending ? (
            <SettingsRow
              title={t("settings.rows.restart")}
              description={t("app.system.restartHint")}
            >
              <Button
                size="sm"
                variant="outline"
                onClick={onRestart}
                disabled={isRestarting}
                className="rounded-full"
              >
                {isRestarting ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                ) : (
                  <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                )}
                {isRestarting ? restartingActionLabel : restartActionLabel}
              </Button>
            </SettingsRow>
          ) : null}
        </SettingsGroup>
      </section>
    </div>
  );
}

function AdvancedSettings({
  form,
  dirty,
  saving,
  requiresRestartPending,
  isNativeHostSurface,
  onChangeForm,
  onSave,
  onRestart,
  isRestarting,
}: {
  form: NetworkSafetySettingsUpdate;
  dirty: boolean;
  saving: boolean;
  requiresRestartPending: boolean;
  isNativeHostSurface: boolean;
  onChangeForm: Dispatch<SetStateAction<NetworkSafetySettingsUpdate>>;
  onSave: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>
          {isNativeHostSurface
            ? tx("settings.sections.hostSafety", "App safety")
            : tx("settings.sections.webuiSafety", "Web safety")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.localServiceAccess", "Local Service Access")}
            description={tx(
              isNativeHostSurface ? "settings.help.localServiceAccessNative" : "settings.help.localServiceAccess",
              isNativeHostSurface
                ? "Allow Full Access shell commands to reach services on this Mac."
                : "Allow Full Access shell commands to reach localhost services.",
            )}
          >
            <ToggleButton
              checked={form.webuiAllowLocalServiceAccess}
              onChange={(webuiAllowLocalServiceAccess) =>
                onChangeForm((prev) => ({ ...prev, webuiAllowLocalServiceAccess }))
              }
              ariaLabel={tx("settings.rows.localServiceAccess", "Local Service Access")}
              label={form.webuiAllowLocalServiceAccess ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.webuiDefaultAccess", "Default access")}
            description={tx(
              isNativeHostSurface ? "settings.help.webuiDefaultAccessNative" : "settings.help.webuiDefaultAccess",
              isNativeHostSurface
                ? "Used by native chats without a project-specific permission."
                : "Used by web chats without a project-specific permission.",
            )}
          >
            <SegmentedControl
              value={form.webuiDefaultAccessMode}
              options={[
                { value: "default", label: tx("settings.values.defaultPermission", "Default Permission") },
                { value: "full", label: tx("settings.values.fullAccess", "Full Access") },
              ]}
              onChange={(webuiDefaultAccessMode) =>
                onChangeForm((prev) => ({
                  ...prev,
                  webuiDefaultAccessMode: webuiDefaultAccessMode as WebuiDefaultAccessMode,
                }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.guardLevel", "Prompt Guard Level")}
            description={tx(
              "settings.help.guardLevel",
              "Control prompt-injection and shell-interception intensity. SSRF and workspace boundary always stay on.",
            )}
          >
            <SegmentedControl
              value={form.guardLevel ?? "standard"}
              options={[
                { value: "standard", label: tx("settings.values.guardStandard", "Standard") },
                { value: "minimal", label: tx("settings.values.guardMinimal", "Minimal") },
                { value: "off", label: tx("settings.values.guardOff", "Off") },
              ]}
              onChange={(guardLevel) =>
                onChangeForm((prev) => ({
                  ...prev,
                  guardLevel: guardLevel as GuardLevel,
                }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.coldStorageDays", "冷门仓库阈值")}
            description={tx(
              "settings.help.coldStorageDays",
              "工具多少天未调用后转入冷门仓库（0=禁用）",
            )}
          >
            <SegmentedControl
              value={String(form.coldStorageDays)}
              options={[
                { value: "0", label: tx("settings.values.coldStorageOff", "禁用") },
                { value: "7", label: tx("settings.values.coldStorage7", "7天") },
                { value: "14", label: tx("settings.values.coldStorage14", "14天") },
                { value: "30", label: tx("settings.values.coldStorage30", "30天") },
              ]}
              onChange={(value) =>
                onChangeForm((prev) => ({
                  ...prev,
                  coldStorageDays: Number(value),
                }))
              }
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.duplicateSimilarityThreshold", "重复检测灵敏度")}
            description={tx(
              "settings.help.duplicateSimilarityThreshold",
              "工具/技能功能相似度超过此阈值时报告为潜在重复",
            )}
          >
            <SegmentedControl
              value={String(form.duplicateSimilarityThreshold)}
              options={[
                { value: "0.4", label: tx("settings.values.duplicateLow", "低") },
                { value: "0.6", label: tx("settings.values.duplicateMedium", "中") },
                { value: "0.8", label: tx("settings.values.duplicateHigh", "高") },
              ]}
              onChange={(value) =>
                onChangeForm((prev) => ({
                  ...prev,
                  duplicateSimilarityThreshold: Number(value),
                }))
              }
            />
          </SettingsRow>
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>

      <p className="max-w-3xl px-1 text-sm leading-6 text-muted-foreground">
        {tx(
          "settings.help.securityManagedControls",
          "Web fetches always protect local, private, and metadata services. Core channel safety stays in config.json.",
        )}
      </p>
    </div>
  );
}

function TimezonePicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (timezone: string) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [query, setQuery] = useState("");
  const options = useMemo(() => timezoneOptions(value), [value]);
  const filteredOptions = useMemo(() => filterTimezoneOptions(options, query), [options, query]);

  return (
    <DropdownMenu onOpenChange={(open) => !open && setQuery("")}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className={cn(
            "h-8 w-[220px] justify-between rounded-full border-input bg-background px-3 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <span className="truncate">{value || tx("settings.timezone.select", "Select timezone")}</span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-[340px] max-w-[calc(100vw-2rem)]"
      >
        <div className="sticky top-0 z-10 bg-popover px-1 pb-1">
          <div className="flex h-9 items-center gap-2 rounded-full border border-input bg-background px-3">
            <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
            <Input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => event.stopPropagation()}
              placeholder={tx("settings.timezone.search", "Search timezone")}
              className="h-7 border-0 bg-transparent px-0 text-[13px] shadow-none focus-visible:ring-0"
            />
          </div>
        </div>
        <div
          className="mt-1 max-h-[18rem] overflow-y-auto pr-0.5 scrollbar-thin scrollbar-track-transparent"
          data-testid="timezone-picker-list"
        >
          {filteredOptions.length ? (
            filteredOptions.map((option) => {
              const selected = option.name === value;
              return (
                <DropdownMenuItem
                  key={option.name}
                  onSelect={() => onChange(option.name)}
                  className={cn(
                    "flex h-9 cursor-default items-center justify-between gap-3 rounded-[12px] px-2.5 text-[13px]",
                    "focus:bg-muted/85 focus:text-foreground",
                    selected && "bg-muted/80 text-foreground focus:bg-muted",
                  )}
                >
                  <span className="min-w-0 truncate font-medium text-foreground">{option.name}</span>
                  <span className="ml-auto flex shrink-0 items-center gap-2">
                    <span className="text-[11.5px] font-medium text-muted-foreground/80">
                      {option.offset}
                    </span>
                    {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
                  </span>
                </DropdownMenuItem>
              );
            })
          ) : (
            <div className="px-3 py-5 text-center text-[12px] text-muted-foreground">
              {tx("settings.timezone.empty", "No matching timezones.")}
            </div>
          )}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ProviderPicker({
  providers,
  value,
  emptyLabel,
  showProviderLogos = false,
  onChange,
}: {
  providers: Array<{ name: string; label: string }>;
  value: string;
  emptyLabel: string;
  showProviderLogos?: boolean;
  onChange: (provider: string) => void;
}) {
  const selectedProvider = providers.find((provider) => provider.name === value) ?? null;
  const disabled = providers.length === 0;

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          className={cn(
            "h-8 w-[210px] justify-between rounded-full border-input bg-background px-3 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
            disabled && "text-muted-foreground",
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            {selectedProvider && showProviderLogos ? (
              <ProviderPickerIcon
                provider={selectedProvider.name}
                showBrandLogos={showProviderLogos}
              />
            ) : null}
            <span className="truncate">{selectedProvider?.label ?? emptyLabel}</span>
          </span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[18rem] w-[240px] overflow-y-auto scrollbar-thin scrollbar-track-transparent"
      >
        {providers.map((provider) => {
          const selected = provider.name === value;
          return (
            <DropdownMenuItem
              key={provider.name}
              onSelect={() => onChange(provider.name)}
              className={cn(
                "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2.5 py-2 text-[13px]",
                "focus:bg-muted/85 focus:text-foreground",
                selected && "bg-muted/80 text-foreground focus:bg-muted",
              )}
            >
              <span className="flex min-w-0 items-center gap-2">
                {showProviderLogos ? (
                  <ProviderPickerIcon
                    provider={provider.name}
                    showBrandLogos={showProviderLogos}
                  />
                ) : null}
                <span className="truncate">{provider.label}</span>
              </span>
              {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ModelIdPicker({
  token,
  settings,
  provider,
  value,
  showProviderLogos,
  onChange,
  providerRows,
}: {
  token: string;
  settings: SettingsPayload;
  provider: string;
  value: string;
  showProviderLogos: boolean;
  onChange: (model: string) => void;
  providerRows?: SettingsPayload["providers"];
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [payload, setPayload] = useState<ProviderModelsPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const effectiveProvider =
    provider === "auto" ? settings.agent.resolved_provider ?? provider : provider;
  const hasConcreteProvider = Boolean(effectiveProvider && effectiveProvider !== "auto");
  const providerRow = providerRows
    ? (providerRows.find((row) => row.name === effectiveProvider) ?? null)
    : settingsProviderRow(settings, effectiveProvider);
  const providerConfigured = providerRows
    ? Boolean(providerRow?.configured)
    : settingsProviderConfigured(settings, effectiveProvider);
  const providerRequiresConfiguration = hasConcreteProvider && !providerConfigured;
  const providerUsesManualModelIds =
    hasConcreteProvider && providerConfigured && providerRow?.auth_type === "oauth";
  const canFetchModels =
    hasConcreteProvider && providerConfigured && !providerUsesManualModelIds;
  const normalizedQuery = query.trim().toLowerCase();
  const providerModels = payload?.models ?? [];
  const visibleModels = providerModels
    .filter((model) => {
      if (!normalizedQuery) return true;
      return [model.id, model.label ?? "", model.owned_by ?? ""]
        .some((field) => field.toLowerCase().includes(normalizedQuery));
    })
    .slice(0, 80);
  const isCatalog = payload?.catalog_kind === "catalog";
  const defersModelList = DEFERRED_MODEL_LIST_PROVIDERS.has(effectiveProvider);
  const hasDeferredSearchQuery =
    normalizedQuery.length >= DEFERRED_MODEL_LIST_QUERY_MIN_LENGTH;
  const shouldFetchModels =
    canFetchModels && (!defersModelList || hasDeferredSearchQuery);
  const waitingForModelSearch =
    open && canFetchModels && defersModelList && !hasDeferredSearchQuery;
  const hasModelList = payload?.status === "available";
  const showModels = Boolean(hasModelList && payload && (!isCatalog || normalizedQuery));
  const customCandidate = query.trim();
  const allowCustomModel = !providerRequiresConfiguration;
  const exactQueryMatch = providerModels.some((model) => model.id === customCandidate);
  const providerModelCount = payload?.model_count ?? providerModels.length;
  const modelUnconfigured = !value.trim() || !providerConfigured;

  useEffect(() => {
    if (!open) return;
    setQuery(providerUsesManualModelIds || !hasConcreteProvider ? value : "");
  }, [open, effectiveProvider, hasConcreteProvider, providerUsesManualModelIds, value]);

  useEffect(() => {
    if (!open || !shouldFetchModels) {
      setPayload(null);
      setError(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setPayload(null);
    setError(null);
    setLoading(true);
    fetchProviderModels(token, effectiveProvider)
      .then((nextPayload) => {
        if (!cancelled) setPayload(nextPayload);
      })
      .catch((err) => {
        if (!cancelled) setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [effectiveProvider, open, shouldFetchModels, token]);

  const selectModel = (model: string) => {
    onChange(model);
    setOpen(false);
  };

  const renderModelRow = (
    model: ProviderModelsPayload["models"][number],
    options: { selected?: boolean } = {},
  ) => (
    <DropdownMenuItem
      key={model.id}
      onSelect={() => selectModel(model.id)}
      className={cn(
        "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2 py-1.5 text-[12px]",
        "focus:bg-muted/85 focus:text-foreground",
        options.selected && "bg-muted/80 text-foreground focus:bg-muted",
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <ProviderPickerIcon
          provider={effectiveProvider}
          showBrandLogos={showProviderLogos}
          unconfigured={!providerConfigured}
        />
        <span className="min-w-0 truncate font-medium text-foreground">
          {model.label ?? model.id}
        </span>
      </span>
      <span className="ml-2 flex shrink-0 items-center gap-2 text-[11px] text-muted-foreground">
        {model.context_window ? <span>{formatContextWindow(model.context_window)}</span> : null}
        {options.selected ? <Check className="h-3.5 w-3.5 text-foreground" aria-hidden /> : null}
      </span>
    </DropdownMenuItem>
  );

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className={cn(
            "h-9 w-[min(360px,70vw)] justify-between rounded-full border-input bg-background px-3 text-[12px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            <ProviderPickerIcon
              provider={effectiveProvider}
              showBrandLogos={showProviderLogos}
              unconfigured={modelUnconfigured}
            />
            <span
              className={cn(
                "min-w-0 truncate font-medium",
                value ? "text-foreground" : "text-muted-foreground",
              )}
            >
              {value || tx("settings.models.selectModel", "Select model")}
            </span>
          </span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-[360px] max-w-[calc(100vw-2rem)] p-1.5"
      >
        <div className="p-1 pb-1.5">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => event.stopPropagation()}
              placeholder={tx("settings.models.searchModels", "Search or type model ID")}
              className="h-8 rounded-full pl-8 pr-3 text-[12px]"
            />
          </div>
        </div>

        {providerRequiresConfiguration ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.providerNotConfigured", "Configure this provider before loading models.")}
          </div>
        ) : providerUsesManualModelIds ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.unsupportedModelList", "Type a model ID manually.")}
          </div>
        ) : !canFetchModels ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.autoProviderCustomOnly", "Auto provider mode uses custom model IDs.")}
          </div>
        ) : waitingForModelSearch ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.searchCatalog", "Search provider catalog to choose a model.")}
          </div>
        ) : loading ? (
          <div className="flex items-center gap-2 px-2 py-1.5 text-[11px] text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            {tx("settings.models.loadingModels", "Loading models...")}
          </div>
        ) : error || payload?.status === "error" ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {payload?.message || error || tx("settings.models.loadFailed", "Model list unavailable.")}
          </div>
        ) : payload?.status === "not_configured" ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.providerNotConfigured", "Configure this provider before loading models.")}
          </div>
        ) : payload?.status === "unsupported" || payload?.status === "missing_api_base" ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {payload.message || tx("settings.models.unsupportedModelList", "Type a model ID manually.")}
          </div>
        ) : isCatalog && !normalizedQuery ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.searchCatalog", "Search provider catalog to choose a model.")}
            {providerModelCount ? ` ${providerModelCount} ${tx("settings.models.modelsAvailable", "available")}.` : ""}
          </div>
        ) : null}

        {showModels && visibleModels.length ? (
          <div className="max-h-[16rem] overflow-y-auto pr-0.5 scrollbar-thin scrollbar-track-transparent">
            {visibleModels.map((model) =>
              renderModelRow(model, { selected: model.id === value }),
            )}
          </div>
        ) : showModels ? (
          <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
            {tx("settings.models.noModelResults", "No matching models.")}
          </div>
        ) : null}

        {allowCustomModel && customCandidate && !exactQueryMatch && customCandidate !== value ? (
          <>
            {showModels ? <DropdownMenuSeparator /> : null}
            <DropdownMenuItem
              onSelect={() => selectModel(customCandidate)}
              className="flex cursor-default items-center gap-2 rounded-[12px] px-2 py-1.5 text-[12px] focus:bg-muted/85"
            >
              <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-muted/80 text-muted-foreground">
                <Pencil className="h-3 w-3" aria-hidden />
              </span>
              <span className="min-w-0 truncate">
                {tx("settings.models.useCustomModel", "Use")}{" "}
                <span className="font-medium text-foreground">“{customCandidate}”</span>
              </span>
            </DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function formatContextWindow(tokens: number): string {
  if (tokens >= 1_000_000) {
    const value = tokens / 1_000_000;
    return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}M`;
  }
  if (tokens >= 1_000) {
    const value = tokens / 1_000;
    return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}K`;
  }
  return String(tokens);
}

function ProviderPickerIcon({
  provider,
  showBrandLogos,
  unconfigured = false,
}: {
  provider: string;
  showBrandLogos: boolean;
  unconfigured?: boolean;
}) {
  const [logoIndex, setLogoIndex] = useState(0);
  const brand = providerBrand(provider);
  const Icon = PROVIDER_ICONS[provider] ?? Hexagon;
  const logoUrl = brand?.logoUrls[logoIndex];

  useEffect(() => setLogoIndex(0), [provider]);

  if (unconfigured) {
    return (
      <span
        data-testid="provider-picker-unconfigured-icon"
        className="grid h-5 w-5 shrink-0 place-items-center text-amber-700 dark:text-amber-200"
        aria-hidden
      >
        <CircleAlert className="h-4 w-4" strokeWidth={1.8} />
      </span>
    );
  }

  if (showBrandLogos && logoUrl) {
    return (
      <span
        data-testid={`provider-picker-logo-${provider}`}
        className="grid h-5 w-5 shrink-0 place-items-center overflow-hidden rounded-md border border-border/35 bg-background shadow-[inset_0_0_0_1px_rgba(0,0,0,0.02)]"
        style={{ boxShadow: `inset 0 0 0 1px ${brand.color}22` }}
        aria-hidden
      >
        <img
          src={logoUrl}
          alt=""
          className="h-3.5 w-3.5 object-contain"
          onError={() => setLogoIndex((index) => index + 1)}
        />
      </span>
    );
  }

  if (showBrandLogos && brand) {
    return (
      <span
        data-testid={`provider-picker-logo-fallback-${provider}`}
        className="grid h-5 w-5 shrink-0 place-items-center rounded-md text-[7.5px] font-semibold text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.18)]"
        style={{ backgroundColor: brand.color }}
        aria-hidden
      >
        {brand.initials}
      </span>
    );
  }

  return (
    <span
      className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"
      aria-hidden
    >
      <Icon className="h-3 w-3" strokeWidth={2} />
    </span>
  );
}

function ProviderSection({
  title,
  count,
  empty,
  children,
}: {
  title: string;
  count: number;
  empty: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3">
      <ByokSectionHeader title={title} count={count} />
      <div className="cyber-glass-panel relative overflow-hidden rounded-[22px]">
        {count > 0 ? (
          <div className="divide-y divide-border/45">{children}</div>
        ) : (
          <ByokEmptyState>{empty}</ByokEmptyState>
        )}
      </div>
    </section>
  );
}

function ByokSectionHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-center justify-between px-1">
      <h2 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
        {title}
      </h2>
      <span className="rounded-full bg-muted px-2 py-0.5 text-[11.5px] font-medium text-muted-foreground">
        {count}
      </span>
    </div>
  );
}

function ByokEmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-[18px] border border-dashed border-border/65 bg-card/45 px-4 py-5 text-[13px] text-muted-foreground">
      {children}
    </div>
  );
}

function ThirdPartyBrandNotice() {
  const { t } = useTranslation();
  return (
    <p className="px-1 text-[11.5px] leading-5 text-muted-foreground/75">
      {t("settings.legal.thirdPartyBrands", {
        defaultValue:
          "Product names, logos, and brands are property of their respective owners. Use is for identification only and does not imply endorsement.",
      })}
    </p>
  );
}

function orderUnconfiguredProviders(
  providers: SettingsPayload["providers"],
): SettingsPayload["providers"] {
  return providers
    .map((provider, index) => ({ provider, index }))
    .sort((left, right) => {
      const rank = providerVisibilityRank(left.provider) - providerVisibilityRank(right.provider);
      return rank || left.index - right.index;
    })
    .map(({ provider }) => provider);
}

function uniqueProviders(
  providers: SettingsPayload["providers"],
): SettingsPayload["providers"] {
  const seen = new Set<string>();
  return providers.filter((provider) => {
    if (seen.has(provider.name)) return false;
    seen.add(provider.name);
    return true;
  });
}

function providerVisibilityRank(provider: SettingsPayload["providers"][number]): number {
  const localRank = LOCAL_UNCONFIGURED_PROVIDER_ORDER.get(provider.name);
  if (localRank !== undefined) return localRank;
  if ((provider.api_key_required ?? true) === false) return 100;
  return 200;
}

function filterProviders(
  providers: SettingsPayload["providers"],
  query: string,
): SettingsPayload["providers"] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return providers;
  return providers.filter((provider) =>
    `${provider.name} ${provider.label} ${provider.api_base ?? ""} ${provider.default_api_base ?? ""}`
      .toLowerCase()
      .includes(normalized),
  );
}

interface TimezoneOption {
  name: string;
  offset: string;
  searchText: string;
}

function timezoneOptions(current: string): TimezoneOption[] {
  return timezonesWithCurrent(current).map((name) => {
    const offset = timezoneOffset(name);
    return {
      name,
      offset,
      searchText: `${name} ${name.replace(/_/g, " ")} ${offset}`.toLowerCase(),
    };
  });
}

function timezonesWithCurrent(current: string): string[] {
  const intl = Intl as typeof Intl & {
    supportedValuesOf?: (key: "timeZone") => string[];
  };
  let values: string[];
  try {
    values = intl.supportedValuesOf?.("timeZone") ?? [];
  } catch {
    values = [];
  }
  const deduped = new Set([...FALLBACK_TIMEZONES, ...values, current].filter(Boolean));
  return Array.from(deduped).sort((left, right) => {
    if (left === "UTC") return -1;
    if (right === "UTC") return 1;
    return left.localeCompare(right);
  });
}

function filterTimezoneOptions(options: TimezoneOption[], query: string): TimezoneOption[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return options;
  return options.filter((option) => option.searchText.includes(normalized));
}

function timezoneOffset(timezone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      timeZoneName: "shortOffset",
      hour: "2-digit",
      minute: "2-digit",
    }).formatToParts(new Date());
    const value = parts.find((part) => part.type === "timeZoneName")?.value;
    return value ? value.replace(/^GMT$/, "UTC").replace(/^GMT/, "UTC") : "UTC";
  } catch {
    return "Custom timezone";
  }
}

function optionRowsWithCurrent(
  options: Array<{ name: string; label: string }>,
  value: string,
): Array<{ name: string; label: string }> {
  if (!value || options.some((option) => option.name === value)) return options;
  return [{ name: value, label: value }, ...options];
}

function modelPresetProviderKey(
  preset: SettingsPayload["model_presets"][number],
  settings: SettingsPayload,
  options: { draftProvider?: string } = {},
): string {
  const provider = options.draftProvider ?? preset.provider;
  if (provider === "auto") {
    return settings.agent.resolved_provider || settings.agent.provider || preset.provider;
  }
  return provider;
}

const PROVIDER_ICONS: Record<string, LucideIcon> = {
  custom: Hexagon,
  openrouter: Sparkles,
  skywork: Sparkles,
  aihubmix: Triangle,
  anthropic: Brain,
  openai: Bot,
  deepseek: Waves,
  zhipu: Grid3X3,
  dashscope: Cloud,
  moonshot: Moon,
  minimax: Zap,
  minimax_anthropic: Brain,
  groq: Cpu,
  huggingface: Layers,
  gemini: Gem,
  mistral: Orbit,
  siliconflow: Layers,
  volcengine: Cloud,
  volcengine_coding_plan: Cloud,
  byteplus: Cloud,
  byteplus_coding_plan: Cloud,
  qianfan: Database,
  ant_ling: Sparkles,
  azure_openai: Cloud,
  bedrock: Database,
  bocha: Search,
  brave: Search,
  duckduckgo: Search,
  exa: Search,
  jina: Search,
  kagi: Search,
  olostep: Search,
  searxng: Search,
  tavily: Search,
  vllm: Cpu,
  ollama: Cpu,
  lm_studio: Cpu,
  atomic_chat: Cpu,
  ovms: Cpu,
  nvidia: Zap,
};

function ProviderIcon({
  provider,
  showBrandLogos,
}: {
  provider: string;
  showBrandLogos: boolean;
}) {
  const [logoIndex, setLogoIndex] = useState(0);
  const brand = providerBrand(provider);
  const Icon = PROVIDER_ICONS[provider] ?? Hexagon;
  const logoUrl = brand?.logoUrls[logoIndex];

  useEffect(() => setLogoIndex(0), [provider]);

  if (showBrandLogos && logoUrl) {
    return (
      <span
        data-testid={`provider-logo-${provider}`}
        className="grid h-10 w-10 shrink-0 place-items-center overflow-hidden rounded-[14px] border border-border/45 bg-background shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)]"
        style={{ boxShadow: `inset 0 0 0 1px ${brand.color}22` }}
      >
        <img
          src={logoUrl}
          alt=""
          className="h-6 w-6 object-contain"
          onError={() => setLogoIndex((index) => index + 1)}
        />
      </span>
    );
  }
  if (showBrandLogos && brand) {
    return (
      <span
        data-testid={`provider-logo-fallback-${provider}`}
        className="grid h-10 w-10 shrink-0 place-items-center rounded-[14px] text-[11px] font-semibold text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.18)]"
        style={{ backgroundColor: brand.color }}
        aria-hidden
      >
        {brand.initials}
      </span>
    );
  }
  return (
    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl bg-muted text-foreground/82 shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)] dark:bg-muted/70">
      <Icon className="h-5 w-5" strokeWidth={2} aria-hidden />
    </span>
  );
}

function OverviewRowIcon({
  icon: Icon,
}: {
  icon: LucideIcon;
}) {
  return (
    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-[12px] bg-muted text-foreground/82 transition-colors group-hover:bg-muted/80 dark:bg-muted/70">
      <Icon className="h-4 w-4" aria-hidden />
    </span>
  );
}

function OverviewValueLogo({
  provider,
  showBrandLogos,
}: {
  provider: string | null | undefined;
  showBrandLogos: boolean;
}) {
  const [logoIndex, setLogoIndex] = useState(0);
  const brand = provider ? providerBrand(provider) : null;
  const logoUrl = brand?.logoUrls[logoIndex];

  useEffect(() => setLogoIndex(0), [provider]);

  if (!provider || !showBrandLogos || !brand) return null;

  if (logoUrl) {
    return (
      <span
        data-testid={`overview-logo-${provider}`}
        className="grid h-5 w-5 shrink-0 place-items-center overflow-hidden rounded-md border border-border/35 bg-background shadow-[inset_0_0_0_1px_rgba(0,0,0,0.02)]"
        style={{ boxShadow: `inset 0 0 0 1px ${brand.color}22` }}
        aria-hidden
      >
        <img
          src={logoUrl}
          alt=""
          className="h-3.5 w-3.5 object-contain"
          onError={() => setLogoIndex((index) => index + 1)}
        />
      </span>
    );
  }

  return (
    <span
      data-testid={`overview-logo-fallback-${provider}`}
      className="grid h-5 w-5 shrink-0 place-items-center rounded-md text-[7.5px] font-semibold text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.18)]"
      style={{ backgroundColor: brand.color }}
      aria-hidden
    >
      {brand.initials}
    </span>
  );
}

function OverviewListRow({
  icon: Icon,
  valueLogoProvider,
  title,
  value,
  caption,
  showBrandLogos = false,
  onClick,
}: {
  icon: LucideIcon;
  valueLogoProvider?: string | null;
  title: string;
  value: string;
  caption: string;
  showBrandLogos?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group flex min-h-[68px] w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-muted/30 sm:px-5"
    >
      <OverviewRowIcon icon={Icon} />
      <span className="min-w-0 flex-1">
        <span className="block text-[14px] font-medium leading-5 text-foreground">{title}</span>
        <span className="mt-0.5 block truncate text-[12px] leading-5 text-muted-foreground">{caption}</span>
      </span>
      <span className="ml-auto flex min-w-0 max-w-[48%] items-center gap-2">
        <OverviewValueLogo provider={valueLogoProvider} showBrandLogos={showBrandLogos} />
        <span className="truncate text-right text-[13px] leading-5 text-muted-foreground">
          {value}
        </span>
        <ChevronRight
          className="h-4 w-4 shrink-0 text-muted-foreground/60 transition-transform group-hover:translate-x-0.5"
          aria-hidden
        />
      </span>
    </button>
  );
}

function ChannelsSettings({
  token,
  onRestart,
  isRestarting,
}: {
  token: string;
  onRestart?: () => void;
  isRestarting?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const [channels, setChannels] = useState<ChannelRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [requiresRestart, setRequiresRestart] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);
  const [configuring, setConfiguring] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const payload = await fetchChannels(token);
      setChannels(payload.channels);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [token]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await fetchChannels(token);
        if (!cancelled) setChannels(payload.channels);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleToggle = async (channel: ChannelRow, enabled: boolean) => {
    if (toggling) return;
    // 首次启用未配置渠道：先弹出凭据表单，而非直接启用。
    if (enabled && !channel.configured && channel.fields.length > 0) {
      setConfiguring(channel.name);
      return;
    }
    setToggling(channel.name);
    try {
      const payload = await updateChannelSettings(token, {
        channel: channel.name,
        enabled,
      });
      setChannels(payload.channels);
      if (payload.requires_restart) setRequiresRestart(true);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setToggling(null);
    }
  };

  if (loading) {
    return (
      <SettingsGroup>
        <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {tx("settings.channels.loading", "加载渠道…")}
        </div>
      </SettingsGroup>
    );
  }

  if (error && !channels) {
    return (
      <SettingsGroup>
        <SettingsRow title={t("settings.status.loadError")}>
          <span className="max-w-[520px] text-sm text-muted-foreground">{error}</span>
        </SettingsRow>
      </SettingsGroup>
    );
  }

  const rows = channels ?? [];

  return (
    <div className="space-y-4">
      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <SettingsGroup>
        {rows.map((channel) => (
          <div key={channel.name}>
            <SettingsRow title={channel.display_name}>
              <div className="flex items-center gap-2.5">
                <StatusPill tone={channel.enabled ? "success" : "neutral"}>
                  {channel.enabled
                    ? tx("settings.channels.enabled", "已启用")
                    : channel.configured
                      ? tx("settings.channels.disabled", "已禁用")
                      : tx("settings.channels.notConfigured", "未配置")}
                </StatusPill>
                <ToggleButton
                  checked={channel.enabled}
                  onChange={(checked) => handleToggle(channel, checked)}
                  ariaLabel={channel.display_name}
                  label={channel.display_name}
                />
              </div>
            </SettingsRow>
            {configuring === channel.name ? (
              <ChannelConfigForm
                channel={channel}
                token={token}
                onCancel={() => setConfiguring(null)}
                onSaved={(payload) => {
                  setChannels(payload.channels);
                  if (payload.requires_restart) setRequiresRestart(true);
                  setConfiguring(null);
                }}
              />
            ) : null}
            {channel.has_qr_login ? (
              <div className="px-4 pb-4 sm:px-5">
                <WechatBridge token={token} onConfirmed={refresh} />
              </div>
            ) : null}
          </div>
        ))}
      </SettingsGroup>

      {requiresRestart ? (
        <RestartSettingsFooter
          dirty={false}
          saving={false}
          pendingRestart
          onSave={() => {}}
          onRestart={onRestart}
          isRestarting={isRestarting}
          pendingMessage={tx(
            "settings.channels.restartHint",
            "渠道启用状态将在网关重启后生效。",
          )}
        />
      ) : null}
    </div>
  );
}

function ChannelConfigForm({
  channel,
  token,
  onCancel,
  onSaved,
}: {
  channel: ChannelRow;
  token: string;
  onCancel: () => void;
  onSaved: (payload: ChannelsPayload) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const [values, setValues] = useState<Record<string, string | boolean>>(() =>
    Object.fromEntries(channel.fields.map((field) => [field.key, field.value])),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const payload = await updateChannelSettings(token, {
        channel: channel.name,
        enabled: true,
        values,
      });
      onSaved(payload);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="px-4 pb-4 sm:px-5">
      <div className="rounded-2xl border border-border/60 bg-muted/30 p-4">
        <div className="mb-1 text-[13px] font-medium">
          {tx("settings.channels.configureTitle", "配置 {name}").replace(
            "{name}",
            channel.display_name,
          )}
        </div>
        <div className="mb-4 text-[12px] text-muted-foreground">
          {tx("settings.channels.configureHelp", "填写接入所需的凭据，保存后启用。")}
        </div>
        <div className="space-y-3">
          {channel.fields.map((field) =>
            field.type === "boolean" ? (
              <div
                key={field.key}
                className="flex items-center justify-between gap-3"
              >
                <span className="text-[13px]">{field.label}</span>
                <ToggleButton
                  checked={Boolean(values[field.key])}
                  onChange={(checked) =>
                    setValues((prev) => ({ ...prev, [field.key]: checked }))
                  }
                  label={field.label}
                />
              </div>
            ) : (
              <label key={field.key} className="block">
                <span className="mb-1 block text-[12px] font-medium text-muted-foreground">
                  {field.label}
                </span>
                <Input
                  type={field.secret ? "password" : "text"}
                  value={String(values[field.key] ?? "")}
                  onChange={(event) =>
                    setValues((prev) => ({
                      ...prev,
                      [field.key]: event.target.value,
                    }))
                  }
                  autoComplete="off"
                  className="h-10 rounded-full px-4 text-[14px]"
                />
              </label>
            ),
          )}
        </div>
        {error ? (
          <div className="mt-3 text-[12px] text-destructive">{error}</div>
        ) : null}
        <div className="mt-4 flex items-center justify-end gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={saving}
          >
            {tx("settings.channels.cancel", "取消")}
          </Button>
          <Button size="sm" onClick={save} disabled={saving}>
            {saving
              ? tx("settings.channels.saving", "保存中…")
              : tx("settings.channels.save", "保存并启用")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SettingsSectionTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
      {children}
    </h2>
  );
}

function SettingsGroup({ children }: { children: ReactNode }) {
  return (
    <div className="cyber-glass-panel relative overflow-hidden rounded-[22px]">
      <div className="divide-y divide-border/45">{children}</div>
    </div>
  );
}

function SettingsRow({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0">
        <div className="text-[14px] font-medium leading-5 text-foreground">{title}</div>
        {description ? (
          <div className="mt-0.5 max-w-[28rem] text-[12px] leading-5 text-muted-foreground">
            {description}
          </div>
        ) : null}
      </div>
      {children ? <div className="min-w-0 sm:ml-6 sm:shrink-0">{children}</div> : null}
    </div>
  );
}

function ReadOnlyRow({
  title,
  value,
  description,
}: {
  title: string;
  value: string;
  description?: string;
}) {
  return (
    <SettingsRow title={title} description={description}>
      <span className="block max-w-full truncate text-left text-[13px] text-muted-foreground sm:max-w-[320px] sm:text-right">
        {value}
      </span>
    </SettingsRow>
  );
}

function ModelPresetPicker({
  presets,
  value,
  settings,
  draftModel,
  draftProvider,
  providerConfigured,
  showProviderLogos,
  onChange,
  onCreateConfiguration,
}: {
  presets: SettingsPayload["model_presets"];
  value: string;
  settings: SettingsPayload;
  draftModel: string;
  draftProvider: string;
  providerConfigured: boolean;
  showProviderLogos: boolean;
  onChange: (preset: string) => void;
  onCreateConfiguration: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const selectedPreset = presets.find((preset) => preset.name === value) ?? presets[0] ?? null;

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild disabled={!presets.length}>
        <Button
          type="button"
          variant="outline"
          aria-label={tx("settings.rows.currentModel", "Current configuration")}
          disabled={!presets.length}
          className={cn(
            "h-12 w-[min(430px,72vw)] justify-between rounded-full border-input bg-background px-3.5 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          {selectedPreset ? (
            <ModelPresetOptionContent
              preset={selectedPreset}
              settings={settings}
              draftModel={draftModel}
              draftProvider={draftProvider}
              forceUnconfigured={selectedPreset?.is_default ? !providerConfigured : undefined}
              showProviderLogos={showProviderLogos}
              compact
            />
          ) : (
            <span className="truncate text-muted-foreground">
              {tx("settings.models.selectModel", "Select model")}
            </span>
          )}
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[20rem] w-[430px] max-w-[calc(100vw-2rem)] overflow-y-auto scrollbar-thin scrollbar-track-transparent"
      >
        {presets.map((preset) => {
          const selected = preset.name === value;
          return (
            <DropdownMenuItem
              key={preset.name}
              onSelect={() => onChange(preset.name)}
              className={cn(
                "flex cursor-default items-center justify-between gap-3 rounded-[12px] px-2.5 py-2 text-[13px]",
                "focus:bg-muted/85 focus:text-foreground",
                selected && "bg-muted/80 text-foreground focus:bg-muted",
              )}
            >
              <ModelPresetOptionContent
                preset={preset}
                settings={settings}
                draftModel={draftModel}
                draftProvider={draftProvider}
                showProviderLogos={showProviderLogos}
              />
              {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
        <div className="mt-1 border-t border-border/55 pt-1">
          <DropdownMenuItem
            onSelect={() => {
              window.setTimeout(onCreateConfiguration, 0);
            }}
            className={cn(
              "flex cursor-default items-center gap-2 rounded-[12px] px-2.5 py-2 text-[13px] font-medium",
              "text-foreground focus:bg-muted/85 focus:text-foreground",
            )}
          >
            <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground">
              <Plus className="h-3.5 w-3.5" aria-hidden />
            </span>
            <span>{tx("settings.models.addConfiguration", "Add configuration")}</span>
          </DropdownMenuItem>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ModelPresetOptionContent({
  preset,
  settings,
  draftModel,
  draftProvider,
  forceUnconfigured,
  showProviderLogos,
  compact = false,
}: {
  preset: SettingsPayload["model_presets"][number];
  settings: SettingsPayload;
  draftModel: string;
  draftProvider: string;
  forceUnconfigured?: boolean;
  showProviderLogos: boolean;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const provider = modelPresetProviderKey(preset, settings, {
    draftProvider: preset.is_default ? draftProvider : undefined,
  });
  const model = preset.is_default ? draftModel : preset.model;
  const providerName = providerDisplayLabel(settings.providers, provider);
  const providerConfigured =
    forceUnconfigured === undefined
      ? settingsProviderConfigured(settings, provider)
      : !forceUnconfigured;
  const title = providerConfigured ? model || preset.label : tx("settings.values.notConfigured", "Not configured");
  const caption = providerConfigured
    ? `${providerName}${preset.label ? ` · ${preset.label}` : ""}`
    : providerName || model || preset.label
      ? [providerName, model || preset.label].filter(Boolean).join(" · ")
      : tx("settings.byok.noConfiguredProviders", "No configured providers");
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <ProviderPickerIcon
        provider={provider}
        showBrandLogos={showProviderLogos}
        unconfigured={!providerConfigured}
      />
      <span className="min-w-0 text-left leading-tight">
        <span
          className={cn(
            "block truncate font-medium",
            providerConfigured ? "text-foreground" : "text-amber-800 dark:text-amber-200",
          )}
        >
          {title}
        </span>
        <span
          className={cn(
            "mt-0.5 block truncate text-muted-foreground",
            compact ? "text-[11.5px]" : "text-[12px]",
          )}
        >
          {caption}
        </span>
      </span>
    </span>
  );
}

function RestartSettingsFooter({
  dirty,
  saving,
  pendingRestart,
  disabled = false,
  message,
  dirtyMessage,
  pendingMessage,
  onSave,
  onRestart,
  onReset,
  isRestarting,
}: {
  dirty: boolean;
  saving: boolean;
  pendingRestart: boolean;
  disabled?: boolean;
  message?: string;
  dirtyMessage?: string;
  pendingMessage?: string;
  onSave: () => void;
  onRestart?: () => void;
  onReset?: () => void;
  isRestarting?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const isNativeHost = getHostApi() !== null;
  const restartLabel = isNativeHost
    ? tx("app.system.restartEngine", "Restart engine")
    : t("app.system.restart");
  const restartingLabel = isNativeHost
    ? tx("app.system.restartingEngine", "Restarting engine...")
    : t("app.system.restarting");
  const statusMessage =
    message ??
    (pendingRestart && !dirty
      ? pendingMessage ?? tx("settings.status.savedRestartApply", "Saved. Restart when ready.")
      : dirty
        ? dirtyMessage ?? t("settings.status.unsaved")
        : undefined);
  const statusTone = disabled ? "danger" : dirty || pendingRestart ? "accent" : undefined;

  return (
    <div className="flex min-h-[58px] flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0 text-[13px] leading-5 text-muted-foreground">
        <SettingsStatusMessage tone={statusTone}>{statusMessage}</SettingsStatusMessage>
      </div>
      <div className="flex w-full shrink-0 flex-wrap justify-end gap-2 sm:w-auto">
        {pendingRestart && !dirty && onRestart ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={onRestart}
            disabled={isRestarting}
            className="rounded-full"
          >
            {isRestarting ? (
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            )}
            {isRestarting ? restartingLabel : restartLabel}
          </Button>
        ) : null}
        {onReset ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={onReset}
            disabled={!dirty || saving}
            className="rounded-full"
          >
            {t("settings.actions.cancel")}
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="outline"
          onClick={onSave}
          disabled={!dirty || disabled || saving}
          className="rounded-full"
        >
          {saving ? t("settings.actions.saving") : t("settings.actions.save")}
        </Button>
      </div>
    </div>
  );
}

function SettingsFooter({
  dirty,
  saving,
  saved,
  disabled = false,
  message,
  onSave,
}: {
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  disabled?: boolean;
  message?: string;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const statusMessage = message ?? (dirty
    ? t("settings.status.unsaved")
    : saved
      ? t("settings.status.savedRestart")
      : tx("settings.status.upToDate", "Up to date."));
  return (
    <div className="flex min-h-[58px] flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="text-[13px] text-muted-foreground">
        <SettingsStatusMessage tone={disabled ? "danger" : dirty || saved ? "accent" : undefined}>
          {statusMessage}
        </SettingsStatusMessage>
      </div>
      <div className="flex justify-end">
        <Button size="sm" variant="outline" onClick={onSave} disabled={!dirty || disabled || saving} className="rounded-full">
          {saving ? t("settings.actions.saving") : t("settings.actions.save")}
        </Button>
      </div>
    </div>
  );
}

function SettingsStatusMessage({
  children,
  tone,
}: {
  children?: ReactNode;
  tone?: "accent" | "danger";
}) {
  if (!children) return null;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2",
        tone === "accent" && "font-medium text-blue-600 dark:text-blue-300",
        tone === "danger" && "font-medium text-destructive",
      )}
    >
      {tone ? (
        <span
          className={cn(
            "h-1.5 w-1.5 shrink-0 rounded-full",
            tone === "accent" &&
              "bg-blue-500 shadow-[0_0_0_3px_rgba(59,130,246,0.14)] dark:bg-blue-400 dark:shadow-[0_0_0_3px_rgba(96,165,250,0.18)]",
            tone === "danger" && "bg-destructive/70",
          )}
          aria-hidden
        />
      ) : null}
      <span>{children}</span>
    </span>
  );
}

function StatusPill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "success" | "warning";
}) {
  return (
    <span
      className={cn(
        "inline-flex max-w-[260px] items-center rounded-full px-2.5 py-1 text-[12px] font-medium",
        tone === "success" && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        tone === "warning" && "bg-amber-500/10 text-amber-700 dark:text-amber-300",
        tone === "neutral" && "bg-muted text-muted-foreground",
      )}
    >
      <span className="truncate">{children}</span>
    </span>
  );
}

function SegmentedControl({
  value,
  options,
  onChange,
}: {
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <div className="inline-flex h-8 items-center rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={cn(
            "rounded-full px-3 py-1 transition-colors",
            value === option.value && "bg-background text-foreground shadow-sm",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function ToggleButton({
  checked,
  onChange,
  ariaLabel,
  label,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  ariaLabel?: string;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel ?? label}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full p-[2px]",
        "transition-colors duration-200 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        checked
          ? "bg-[#2997FF] shadow-[inset_0_0_0_1px_rgba(0,0,0,0.035)]"
          : "bg-muted shadow-[inset_0_0_0_1px_rgba(0,0,0,0.035)] hover:bg-muted/80",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "h-[18px] w-[18px] rounded-full bg-background shadow-[0_1px_2px_rgba(0,0,0,0.18),0_2px_7px_rgba(0,0,0,0.11)]",
          "transition-transform duration-200 ease-out",
          checked ? "translate-x-[16px]" : "translate-x-0",
        )}
      />
      <span className="sr-only">{label}</span>
    </button>
  );
}

function NumberInput({
  value,
  min,
  max,
  onChange,
  suffix,
}: {
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  suffix?: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <Input
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(event) => {
          const parsed = Number(event.target.value);
          if (Number.isFinite(parsed)) onChange(parsed);
        }}
        className="h-8 w-24 max-w-full rounded-full text-[13px]"
      />
      {suffix ? <span className="text-[12px] text-muted-foreground">{suffix}</span> : null}
    </div>
  );
}
