import type {
  Asset,
  AutomationsPayload,
  AutomationUpdatePayload,
  CapabilitiesPayload,
  CapabilityDetail,
  ChatSummary,
  Employee,
  EmployeesPayload,
  CliAppsPayload,
  DocumentPreviewPayload,
  FilePreviewPayload,
  ImageGenerationSettingsUpdate,
  KnowledgeDocument,
  McpPresetsPayload,
  ModelConfigurationCreate,
  ModelConfigurationUpdate,
  NetworkSafetySettingsUpdate,
  ProviderModelsPayload,
  ProviderSettingsUpdate,
  ScreenshotSettingsUpdate,
  SessionDeleteResult,
  SystemIoSettingsUpdate,
  SessionAutomationsPayload,
  SettingsPayload,
  SettingsUpdate,
  SetupCompletePayload,
  SetupValues,
  SidebarStatePayload,
  SkillDetail,
  SkillsPayload,
  SlashCommand,
  TalentCatalogPayload,
  TranscriptionSettingsUpdate,
  TtsSettingsUpdate,
  VideoGenerationSettingsUpdate,
  WebSearchSettingsUpdate,
  WorkspacesPayload,
  WebuiThreadPersistedPayload,
  WorkspaceScopePayload,
} from "./types";
import { fetchWithTimeout } from "./http";

const API_READ_TIMEOUT_MS = 20_000;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
  timeoutMs: number = 0,
): Promise<T> {
  const res = await fetchWithTimeout(
    url,
    {
      ...(init ?? {}),
      headers: {
        ...(init?.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
      credentials: "same-origin",
    },
    timeoutMs,
  );
  if (!res.ok) {
    const text = typeof res.text === "function" ? (await res.text()).trim() : "";
    throw new ApiError(res.status, text || `HTTP ${res.status}`);
  }
  const contentType = res.headers?.get?.("content-type") ?? "";
  if (contentType && !contentType.toLowerCase().includes("application/json")) {
    const text = typeof res.text === "function" ? await res.text() : "";
    const isHtml = text.trimStart().toLowerCase().startsWith("<!doctype");
    throw new ApiError(
      res.status,
      isHtml
        ? "Gateway returned WebUI HTML instead of JSON. Restart biscuitbot gateway and try again."
        : "Gateway returned a non-JSON response.",
    );
  }
  return (await res.json()) as T;
}

function mcpValuesHeader(values: Record<string, unknown>): HeadersInit | undefined {
  const payload: Record<string, unknown> = {};
  Object.entries(values).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) payload[key] = trimmed;
      return;
    }
    payload[key] = value;
  });
  if (!Object.keys(payload).length) return undefined;
  return { "X-Biscuitbot-MCP-Values": JSON.stringify(payload) };
}

function automationValuesHeader(values: AutomationUpdatePayload): HeadersInit {
  return { "X-Biscuitbot-Automation-Values": encodeURIComponent(JSON.stringify(values)) };
}

/** 数字人员工的创建/更新字段（均可选；后端负责必填校验与归一化）。 */
export interface EmployeeValues {
  id?: string;
  name?: string;
  title?: string;
  avatar?: string;
  system_prompt?: string;
  skills?: string[];
  enabled?: boolean;
}

function employeeValuesHeader(values: EmployeeValues): HeadersInit {
  return { "X-Biscuitbot-Employee-Values": encodeURIComponent(JSON.stringify(values)) };
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
    run_started_at?: number | null;
    workspace_scope?: WorkspaceScopePayload | null;
    employee?: string | null;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
    runStartedAt: s.run_started_at ?? null,
    workspaceScope: s.workspace_scope ?? null,
    employee: s.employee ?? null,
  }));
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export interface FetchWebuiThreadOptions {
  limit?: number;
  direction?: "latest";
  before?: string | null;
}

export async function fetchWebuiThread(
  token: string,
  key: string,
  optionsOrBase?: FetchWebuiThreadOptions | string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const options = typeof optionsOrBase === "string" ? undefined : optionsOrBase;
  const resolvedBase = typeof optionsOrBase === "string" ? optionsOrBase : base;
  const params = new URLSearchParams();
  if (options?.limit !== undefined) params.set("limit", String(options.limit));
  if (options?.direction) params.set("direction", options.direction);
  if (options?.before) params.set("before", options.before);
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  const url = `${resolvedBase}/api/sessions/${encodeURIComponent(key)}/webui-thread${suffix}`;
  const res = await fetchWithTimeout(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function fetchFilePreview(
  token: string,
  key: string,
  path: string,
  base: string = "",
): Promise<FilePreviewPayload> {
  const query = new URLSearchParams();
  query.set("path", path);
  return request<FilePreviewPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/file-preview?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSessionAutomations(
  token: string,
  key: string,
  base: string = "",
): Promise<SessionAutomationsPayload> {
  return request<SessionAutomationsPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/automations`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchAutomations(
  token: string,
  base: string = "",
): Promise<AutomationsPayload> {
  return request<AutomationsPayload>(
    `${base}/api/webui/automations`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runAutomationAction(
  token: string,
  action: "enable" | "disable" | "delete" | "run",
  id: string,
  base: string = "",
): Promise<AutomationsPayload> {
  const query = new URLSearchParams();
  query.set("id", id);
  return request<AutomationsPayload>(
    `${base}/api/webui/automations/${action}?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateAutomation(
  token: string,
  id: string,
  values: AutomationUpdatePayload,
  base: string = "",
): Promise<AutomationsPayload> {
  const query = new URLSearchParams();
  query.set("id", id);
  return request<AutomationsPayload>(
    `${base}/api/webui/automations/update?${query}`,
    token,
    {
      headers: automationValuesHeader(values),
    },
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSkills(
  token: string,
  base: string = "",
): Promise<SkillsPayload> {
  return request<SkillsPayload>(
    `${base}/api/webui/skills`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSkillDetail(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillDetail> {
  return request<SkillDetail>(
    `${base}/api/webui/skills/${encodeURIComponent(name)}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function deleteSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<{ deleted: boolean; name: string }> {
  return request<{ deleted: boolean; name: string }>(
    `${base}/api/webui/skills/${encodeURIComponent(name)}/delete`,
    token,
  );
}

/** 统一能力目录（``kind`` 可选过滤 runtime：prompt / process / mcp）。 */
export async function fetchCapabilities(
  token: string,
  kind?: string,
  base: string = "",
): Promise<CapabilitiesPayload> {
  const query = new URLSearchParams();
  if (kind) query.set("kind", kind);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<CapabilitiesPayload>(
    `${base}/api/webui/capabilities${suffix}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchCapabilityDetail(
  token: string,
  id: string,
  base: string = "",
): Promise<CapabilityDetail> {
  return request<CapabilityDetail>(
    `${base}/api/webui/capabilities/${encodeURIComponent(id)}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchEmployees(
  token: string,
  base: string = "",
): Promise<EmployeesPayload> {
  return request<EmployeesPayload>(
    `${base}/api/webui/employees`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function createEmployee(
  token: string,
  values: EmployeeValues,
  base: string = "",
): Promise<Employee> {
  return request<Employee>(
    `${base}/api/webui/employees/create`,
    token,
    { headers: employeeValuesHeader(values) },
    API_READ_TIMEOUT_MS,
  );
}

export async function updateEmployee(
  token: string,
  id: string,
  values: EmployeeValues,
  base: string = "",
): Promise<Employee> {
  const query = new URLSearchParams();
  query.set("id", id);
  return request<Employee>(
    `${base}/api/webui/employees/update?${query}`,
    token,
    { headers: employeeValuesHeader(values) },
    API_READ_TIMEOUT_MS,
  );
}

export async function deleteEmployee(
  token: string,
  id: string,
  base: string = "",
): Promise<{ deleted: boolean; id: string }> {
  return request<{ deleted: boolean; id: string }>(
    `${base}/api/webui/employees/${encodeURIComponent(id)}/delete`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/** 拉取人才市场注册表目录（注册表 URL 由后台配置；``refresh=true`` 强制重新拉取）。 */
export async function fetchTalentCatalog(
  token: string,
  base: string = "",
  refresh = false,
): Promise<TalentCatalogPayload> {
  const query = new URLSearchParams();
  if (refresh) query.set("refresh", "1");
  return request<TalentCatalogPayload>(
    `${base}/api/webui/talent-market/catalog?${query.toString()}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

/** 从人才市场安装（下载）一名数字员工；重复 id 后端幂等返回已存在记录。 */
export async function installTalentEmployee(
  token: string,
  values: EmployeeValues,
  sourceUrl?: string,
  base: string = "",
): Promise<Employee & { already_existed?: boolean }> {
  const query = new URLSearchParams();
  if (sourceUrl) query.set("source_url", sourceUrl);
  return request<Employee & { already_existed?: boolean }>(
    `${base}/api/webui/talent-market/install?${query.toString()}`,
    token,
    { headers: employeeValuesHeader(values) },
    API_READ_TIMEOUT_MS,
  );
}

export async function deleteSession(
  token: string,
  key: string,
  optionsOrBase?: { deleteAutomations?: boolean } | string,
  base: string = "",
): Promise<SessionDeleteResult> {
  const options = typeof optionsOrBase === "string" ? undefined : optionsOrBase;
  const resolvedBase = typeof optionsOrBase === "string" ? optionsOrBase : base;
  const query = new URLSearchParams();
  if (options?.deleteAutomations) query.set("delete_automations", "true");
  const suffix = query.toString() ? `?${query}` : "";
  return request<SessionDeleteResult>(
    `${resolvedBase}/api/sessions/${encodeURIComponent(key)}/delete${suffix}`,
    token,
  );
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSettingsUsage(
  token: string,
  base: string = "",
): Promise<NonNullable<SettingsPayload["usage"]>> {
  return request<NonNullable<SettingsPayload["usage"]>>(
    `${base}/api/settings/usage`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export interface VersionCheckResult {
  updateAvailable: {
    currentVersion: string;
    latestVersion: string;
    pypiUrl?: string;
  } | null;
}

export async function checkVersion(
  token: string,
  base: string = "",
): Promise<VersionCheckResult> {
  return request<VersionCheckResult>(
    `${base}/api/settings/version-check`,
    token,
    undefined,
    10_000,
  );
}

export interface SelfUpdateResult {
  selfUpdate: {
    success: boolean;
    newVersion?: string;
    output?: string;
  };
  requires_restart?: boolean;
}

export async function selfUpdate(
  token: string,
  base: string = "",
): Promise<SelfUpdateResult> {
  return request<SelfUpdateResult>(
    `${base}/api/settings/self-update`,
    token,
    undefined,
    120_000,
  );
}

export async function fetchWorkspaces(
  token: string,
  base: string = "",
): Promise<WorkspacesPayload> {
  return request<WorkspacesPayload>(
    `${base}/api/workspaces`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchInstalledCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps?installed_only=1`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runCliAppAction(
  token: string,
  action: "install" | "update" | "uninstall" | "test",
  name: string,
  base: string = "",
): Promise<CliAppsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<CliAppsPayload>(`${base}/api/settings/cli-apps/${action}?${query}`, token);
}

export async function fetchMcpPresets(
  token: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchProviderModels(
  token: string,
  provider: string,
  base: string = "",
): Promise<ProviderModelsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<ProviderModelsPayload>(
    `${base}/api/settings/provider-models?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runMcpPresetAction(
  token: string,
  action: "enable" | "remove" | "test",
  name: string,
  values: Record<string, string> = {},
  base: string = "",
): Promise<McpPresetsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/${action}?${query}`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function saveCustomMcpServer(
  token: string,
  values: Record<string, string>,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/custom`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function importMcpConfig(
  token: string,
  config: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/import`,
    token,
    { headers: mcpValuesHeader({ config }) },
  );
}

export async function updateMcpServerTools(
  token: string,
  name: string,
  enabledTools: string[],
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/tools`,
    token,
    { headers: mcpValuesHeader({ name, enabled_tools: enabledTools }) },
  );
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
  };
  const body = await request<{ commands: Row[] }>(
    `${base}/api/commands`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.commands
    .filter((command) => !["/stop", "/restart"].includes(command.command))
    .map((command) => ({
      command: command.command,
      title: command.title,
      description: command.description,
      icon: command.icon,
      argHint: command.arg_hint ?? "",
    }));
}

export async function fetchSidebarState(
  token: string,
  base: string = "",
): Promise<SidebarStatePayload> {
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateSidebarState(
  token: string,
  state: SidebarStatePayload,
  base: string = "",
): Promise<SidebarStatePayload> {
  const query = new URLSearchParams();
  query.set("state", JSON.stringify(state));
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state/update?${query}`,
    token,
  );
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.modelPreset !== undefined) {
    query.set("model_preset", update.modelPreset ?? "default");
  }
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  if (update.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(update.contextWindowTokens));
  }
  if (update.timezone !== undefined) query.set("timezone", update.timezone);
  if (update.botName !== undefined) query.set("bot_name", update.botName);
  if (update.botIcon !== undefined) query.set("bot_icon", update.botIcon);
  if (update.toolHintMaxLength !== undefined) {
    query.set("tool_hint_max_length", String(update.toolHintMaxLength));
  }
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

export async function createModelConfiguration(
  token: string,
  configuration: ModelConfigurationCreate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (configuration.name !== undefined) query.set("name", configuration.name);
  query.set("label", configuration.label);
  query.set("provider", configuration.provider);
  query.set("model", configuration.model);
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/create?${query}`,
    token,
  );
}

export async function updateModelConfiguration(
  token: string,
  configuration: ModelConfigurationUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("name", configuration.name);
  if (configuration.label !== undefined) query.set("label", configuration.label);
  if (configuration.provider !== undefined) query.set("provider", configuration.provider);
  if (configuration.model !== undefined) query.set("model", configuration.model);
  if (configuration.contextWindowTokens !== undefined) {
    query.set("context_window_tokens", String(configuration.contextWindowTokens));
  }
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/update?${query}`,
    token,
  );
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.apiBase !== undefined) query.set("api_base", update.apiBase);
  if (update.apiType !== undefined) query.set("api_type", update.apiType);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/update?${query}`,
    token,
  );
}

/** Complete the first-run welcome setup by persisting a provider + API key. */
export async function completeSetup(
  token: string,
  values: SetupValues,
  base: string = "",
): Promise<SetupCompletePayload> {
  const query = new URLSearchParams();
  query.set("provider", values.provider);
  query.set("api_key", values.apiKey);
  if (values.apiBase) query.set("api_base", values.apiBase);
  if (values.model) query.set("model", values.model);
  return request<SetupCompletePayload>(
    `${base}/api/webui/setup/complete?${query}`,
    token,
  );
}

/** 请求后端重启引擎（打包桌面端兜底：宿主未注入 biscuitbotHost 时使用）。 */
export async function requestEngineRestart(
  token: string,
  base: string = "",
): Promise<void> {
  await request<{ ok: boolean }>(`${base}/api/desktop/restart`, token);
}

/** 微信扫码登录：一张登录二维码（qrcode_id 用于后续轮询）。 */
export interface WeixinLoginQr {
  qrcode_id: string;
  qr_content: string;
}

/** 微信扫码登录：单次轮询结果，状态机同原生 weixin 渠道。 */
export interface WeixinLoginStatus {
  status: string;
  confirmed?: boolean;
  expired?: boolean;
  enabled?: boolean;
  bot_id?: string;
  user_id?: string;
  error?: string;
}

export async function fetchWeixinLoginQr(
  token: string,
  base: string = "",
): Promise<WeixinLoginQr> {
  return request<WeixinLoginQr>(`${base}/api/webui/weixin/login-qr`, token);
}

export async function pollWeixinLoginStatus(
  token: string,
  qrcodeId: string,
  base: string = "",
): Promise<WeixinLoginStatus> {
  const query = new URLSearchParams();
  query.set("qrcode", qrcodeId);
  return request<WeixinLoginStatus>(
    `${base}/api/webui/weixin/login-status?${query}`,
    token,
  );
}

/** 渠道配置表单字段（后端从渠道 default_config 推导出的凭据字段）。 */
export interface ChannelField {
  key: string;
  label: string;
  type: "string" | "boolean";
  secret: boolean;
  value: string | boolean;
}

/** 单个渠道：名称 + 展示名 + 启用/已配置状态 + 配置表单字段。 */
export interface ChannelRow {
  name: string;
  display_name: string;
  enabled: boolean;
  configured: boolean;
  has_qr_login: boolean;
  fields: ChannelField[];
}

/** 渠道列表载荷。 */
export interface ChannelsPayload {
  channels: ChannelRow[];
  requires_restart?: boolean;
}

export async function fetchChannels(
  token: string,
  base: string = "",
): Promise<ChannelsPayload> {
  return request<ChannelsPayload>(`${base}/api/settings/channels`, token);
}

export async function updateChannelSettings(
  token: string,
  update: {
    channel: string;
    enabled: boolean;
    values?: Record<string, string | boolean>;
  },
  base: string = "",
): Promise<ChannelsPayload> {
  const query = new URLSearchParams();
  query.set("channel", update.channel);
  query.set("enabled", String(update.enabled));
  if (update.values) {
    for (const [key, value] of Object.entries(update.values)) {
      query.set(key, String(value));
    }
  }
  return request<ChannelsPayload>(
    `${base}/api/settings/channels/update?${query}`,
    token,
  );
}

/** 知识库文档列表载荷。 */
export interface KnowledgeDocumentsPayload {
  documents: KnowledgeDocument[];
}

export async function fetchKnowledgeDocuments(
  token: string,
  base: string = "",
): Promise<KnowledgeDocumentsPayload> {
  return request<KnowledgeDocumentsPayload>(`${base}/api/settings/knowledge`, token);
}

export async function deleteKnowledgeDocument(
  token: string,
  name: string,
  base: string = "",
): Promise<KnowledgeDocumentsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<KnowledgeDocumentsPayload>(
    `${base}/api/settings/knowledge/delete?${query}`,
    token,
  );
}

/** 生成资产（图片/视频/音频）列表载荷。 */
export interface AssetsPayload {
  assets: Asset[];
}

export async function fetchAssets(
  token: string,
  base: string = "",
): Promise<AssetsPayload> {
  return request<AssetsPayload>(`${base}/api/settings/assets`, token);
}

export async function deleteAsset(
  token: string,
  id: string,
  base: string = "",
): Promise<AssetsPayload> {
  const query = new URLSearchParams();
  query.set("id", id);
  return request<AssetsPayload>(`${base}/api/settings/assets/delete?${query}`, token);
}

export async function fetchDocumentPreview(
  token: string,
  id: string,
  base: string = "",
): Promise<DocumentPreviewPayload> {
  const query = new URLSearchParams();
  query.set("id", id);
  return request<DocumentPreviewPayload>(
    `${base}/api/settings/assets/preview?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function loginProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/oauth-login?${query}`,
    token,
  );
}

export async function logoutProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/oauth-logout?${query}`,
    token,
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.baseUrl !== undefined) query.set("base_url", update.baseUrl);
  if (update.maxResults !== undefined) query.set("max_results", String(update.maxResults));
  if (update.timeout !== undefined) query.set("timeout", String(update.timeout));
  if (update.useJinaReader !== undefined) {
    query.set("use_jina_reader", String(update.useJinaReader));
  }
  return request<SettingsPayload>(
    `${base}/api/settings/web-search/update?${query}`,
    token,
  );
}

export async function updateNetworkSafetySettings(
  token: string,
  update: NetworkSafetySettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("webui_allow_local_service_access", String(update.webuiAllowLocalServiceAccess));
  query.set("webui_default_access_mode", update.webuiDefaultAccessMode);
  if (update.guardLevel !== undefined) query.set("guard_level", update.guardLevel);
  if (update.coldStorageDays !== undefined) query.set("cold_storage_days", String(update.coldStorageDays));
  if (update.duplicateSimilarityThreshold !== undefined) query.set("duplicate_similarity_threshold", String(update.duplicateSimilarityThreshold));
  return request<SettingsPayload>(
    `${base}/api/settings/network-safety/update?${query}`,
    token,
  );
}

export async function updateImageGenerationSettings(
  token: string,
  update: ImageGenerationSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("default_aspect_ratio", update.defaultAspectRatio);
  query.set("default_image_size", update.defaultImageSize);
  query.set("max_images_per_turn", String(update.maxImagesPerTurn));
  return request<SettingsPayload>(
    `${base}/api/settings/image-generation/update?${query}`,
    token,
  );
}

export async function updateVideoGenerationSettings(
  token: string,
  update: VideoGenerationSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("default_ratio", update.defaultRatio);
  query.set("default_duration", String(update.defaultDuration));
  query.set("default_resolution", update.defaultResolution);
  query.set("generate_audio", String(update.generateAudio));
  query.set("watermark", String(update.watermark));
  return request<SettingsPayload>(
    `${base}/api/settings/video-generation/update?${query}`,
    token,
  );
}

export async function updateScreenshotSettings(
  token: string,
  update: ScreenshotSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  if (update.visionModel) {
    query.set("visionModel", update.visionModel);
  }
  if (update.visionModelOverride) {
    query.set("visionModelOverride", update.visionModelOverride);
  }
  query.set("maxWidth", String(update.maxWidth));
  query.set("maxHeight", String(update.maxHeight));
  query.set("quality", String(update.quality));
  return request<SettingsPayload>(
    `${base}/api/settings/screenshot/update?${query}`,
    token,
  );
}

export async function updateSystemIoSettings(
  token: string,
  update: SystemIoSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("allowActions", update.allowActions.join(","));
  return request<SettingsPayload>(
    `${base}/api/settings/system-io/update?${query}`,
    token,
  );
}

export async function updateTranscriptionSettings(
  token: string,
  update: TranscriptionSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("language", update.language);
  query.set("max_duration_sec", String(update.maxDurationSec));
  query.set("max_upload_mb", String(update.maxUploadMb));
  return request<SettingsPayload>(
    `${base}/api/settings/transcription/update?${query}`,
    token,
  );
}

export async function updateTtsSettings(
  token: string,
  update: TtsSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("voice", update.voice);
  query.set("rate", update.rate);
  return request<SettingsPayload>(
    `${base}/api/settings/tts/update?${query}`,
    token,
  );
}
