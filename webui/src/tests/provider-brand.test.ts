import { describe, expect, it } from "vitest";

import { faviconUrls, logoFallbackUrls, providerBrand, inferProviderFromModelName } from "@/lib/provider-brand";

describe("provider brand logos", () => {
  it("uses multiple favicon sources before falling back to initials", () => {
    expect(faviconUrls("dashscope.aliyun.com")).toEqual([
      "https://dashscope.aliyun.com/favicon.ico",
      "https://icons.duckduckgo.com/ip3/dashscope.aliyun.com.ico",
      "https://www.google.com/s2/favicons?domain=dashscope.aliyun.com&sz=64",
    ]);
  });

  it("keeps explicit Google favicon URLs first before trying fallbacks", () => {
    expect(logoFallbackUrls("https://www.google.com/s2/favicons?domain=browserbase.com&sz=64")).toEqual([
      "https://www.google.com/s2/favicons?domain=browserbase.com&sz=64",
      "https://browserbase.com/favicon.ico",
      "https://icons.duckduckgo.com/ip3/browserbase.com.ico",
    ]);
  });

  it("normalizes path-like favicon domains for secondary fallbacks", () => {
    expect(logoFallbackUrls("https://www.google.com/s2/favicons?domain=github.com/biscuitbot/biscuitbot&sz=64")).toEqual([
      "https://www.google.com/s2/favicons?domain=github.com/biscuitbot/biscuitbot&sz=64",
      "https://github.com/favicon.ico",
      "https://icons.duckduckgo.com/ip3/github.com.ico",
      "https://www.google.com/s2/favicons?domain=github.com%2Fbiscuitbot%2Fbiscuitbot&sz=64",
    ]);
  });
});

describe("provider brand for each supported provider", () => {
  it("returns Anthropic brand", () => {
    const brand = providerBrand("anthropic");
    expect(brand?.color).toBe("#D97757");
    expect(brand?.initials).toBe("A");
    expect(brand?.logoUrls).toContain("https://anthropic.com/favicon.ico");
  });

  it("returns OpenAI brand", () => {
    const brand = providerBrand("openai");
    expect(brand?.initials).toBe("AI");
    expect(brand?.logoUrls).toContain("https://openai.com/favicon.ico");
  });

  it("returns DeepSeek brand", () => {
    const brand = providerBrand("deepseek");
    expect(brand?.color).toBe("#4D6BFE");
    expect(brand?.initials).toBe("D");
    expect(brand?.logoUrls).toContain("https://deepseek.com/favicon.ico");
  });

  it("returns DashScope (阿里通义) brand", () => {
    const brand = providerBrand("dashscope");
    expect(brand?.color).toBe("#FF6A00");
    expect(brand?.initials).toBe("DS");
    expect(brand?.logoUrls).toContain("https://dashscope.aliyun.com/favicon.ico");
  });

  it("returns Ollama brand", () => {
    const brand = providerBrand("ollama");
    expect(brand?.initials).toBe("O");
    expect(brand?.logoUrls).toContain("https://ollama.com/favicon.ico");
  });

  it("returns Custom brand", () => {
    const brand = providerBrand("custom");
    expect(brand?.color).toBe("#6B7280");
    expect(brand?.initials).toBe("C");
  });

  it("returns Zhipu AI (智谱) brand", () => {
    const brand = providerBrand("zhipu");
    expect(brand?.color).toBe("#0A87C6");
    expect(brand?.initials).toBe("Z");
    expect(brand?.logoUrls).toContain("https://zhipuai.cn/favicon.ico");
  });

  it("returns Moonshot (Kimi) brand", () => {
    const brand = providerBrand("moonshot");
    expect(brand?.color).toBe("#6B57D9");
    expect(brand?.initials).toBe("K");
    expect(brand?.logoUrls).toContain("https://moonshot.cn/favicon.ico");
  });

  it("returns Step Fun (阶跃星辰) brand", () => {
    const brand = providerBrand("stepfun");
    expect(brand?.color).toBe("#10B981");
    expect(brand?.initials).toBe("S");
    expect(brand?.logoUrls).toContain("https://stepfun.com/favicon.ico");
  });

  it("returns null for unknown provider", () => {
    expect(providerBrand("unknown_provider")).toBeNull();
    expect(providerBrand(null)).toBeNull();
    expect(providerBrand(undefined)).toBeNull();
  });
});

describe("inferProviderFromModelName", () => {
  it("infers anthropic from claude model names", () => {
    expect(inferProviderFromModelName("claude-sonnet-4")).toBe("anthropic");
    expect(inferProviderFromModelName("anthropic/claude-opus-4-5")).toBe("anthropic");
  });

  it("infers openai from gpt/o model names", () => {
    expect(inferProviderFromModelName("gpt-4o")).toBe("openai");
    expect(inferProviderFromModelName("o3")).toBe("openai");
    expect(inferProviderFromModelName("openai/gpt-4o-mini")).toBe("openai");
  });

  it("infers deepseek from deepseek model names", () => {
    expect(inferProviderFromModelName("deepseek-v4-pro")).toBe("deepseek");
    expect(inferProviderFromModelName("deepseek/deepseek-r1")).toBe("deepseek");
  });

  it("infers dashscope from qwen/tongyi model names", () => {
    expect(inferProviderFromModelName("qwen-max")).toBe("dashscope");
    expect(inferProviderFromModelName("dashscope/qwen-plus")).toBe("dashscope");
    expect(inferProviderFromModelName("tongyi/qwen-coder")).toBe("dashscope");
  });

  it("infers ollama from local model names", () => {
    expect(inferProviderFromModelName("ollama/llama3")).toBe("ollama");
    expect(inferProviderFromModelName("llama3:latest")).toBe("ollama");
  });

  it("infers zhipu from glm/zhipu model names", () => {
    expect(inferProviderFromModelName("glm-4")).toBe("zhipu");
    expect(inferProviderFromModelName("zhipu/glm-4-flash")).toBe("zhipu");
    expect(inferProviderFromModelName("chatglm-4")).toBe("zhipu");
  });

  it("infers moonshot from kimi/moonshot model names", () => {
    expect(inferProviderFromModelName("moonshot/kimi-k2.5")).toBe("moonshot");
    expect(inferProviderFromModelName("kimi-k2.5")).toBe("moonshot");
  });

  it("infers stepfun from step model names", () => {
    expect(inferProviderFromModelName("stepfun/step-2-16k")).toBe("stepfun");
    expect(inferProviderFromModelName("step-2-16k")).toBe("stepfun");
    expect(inferProviderFromModelName("step-1.5-flash")).toBe("stepfun");
  });

  it("returns null for unknown models", () => {
    expect(inferProviderFromModelName(null)).toBeNull();
    expect(inferProviderFromModelName("")).toBeNull();
    expect(inferProviderFromModelName("some-random-model")).toBeNull();
  });
});
