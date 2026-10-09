import type { VideoGenerationSettings } from "@/lib/types";

/**
 * 视频生成 payload 测试夹具（厂商自包含结构）。
 *
 * 与后端 `settings_payload()` 的 `video_generation` 段同形：各厂商各自的
 * 默认参数 + 能力/选项数据（含卡片展示信息 display_name / provider /
 * resolution_optional）。`overrides` 可按厂商局部覆盖 vendors 段。
 */
export function videoGenerationPayload(
  overrides: Partial<VideoGenerationSettings["vendors"]> = {},
): VideoGenerationSettings {
  return {
    vendors: {
      seedance: {
        configured: false,
        display_name: "Seedance",
        provider: "volcengine",
        resolution_optional: true,
        model: "doubao-seedance",
        default_ratio: "16:9",
        default_duration: 6,
        default_resolution: null,
        generate_audio: true,
        seed: null,
        watermark: false,
        save_dir: "generated/videos",
        ...overrides.seedance,
      },
      kling: {
        configured: false,
        display_name: "可灵",
        provider: "kling",
        resolution_optional: true,
        model: "kling-3.0",
        default_ratio: "16:9",
        default_duration: 5,
        default_resolution: null,
        generate_audio: true,
        save_dir: "generated/videos",
        ...overrides.kling,
      },
      minimax: {
        configured: false,
        display_name: "MiniMax",
        provider: "minimax",
        resolution_optional: false,
        model: "MiniMax-H3",
        default_ratio: "16:9",
        default_duration: 5,
        default_resolution: "768P",
        watermark: false,
        save_dir: "generated/videos",
        ...overrides.minimax,
      },
      dashscope: {
        configured: false,
        display_name: "通义万相（灵积）",
        provider: "dashscope",
        resolution_optional: true,
        model: "wan2.6-t2v",
        default_ratio: "16:9",
        default_duration: 5,
        default_resolution: "720P",
        watermark: false,
        save_dir: "generated/videos",
        ...overrides.dashscope,
      },
    },
    support: {
      seedance: ["t2v", "i2v", "v2v", "ref_image", "ref_audio"],
      kling: ["t2v", "i2v", "v2v"],
      minimax: ["t2v", "i2v", "v2v", "ref_image", "ref_audio"],
      dashscope: ["t2v", "i2v"],
    },
    ratio_options: {
      seedance: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"],
      kling: ["16:9", "9:16", "1:1"],
      minimax: ["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
      dashscope: ["16:9", "9:16", "1:1"],
    },
    resolution_options: {
      seedance: ["480p", "720p", "1080p", "4K"],
      kling: ["720p", "1080p", "4k"],
      minimax: ["768P", "2K"],
      dashscope: ["480P", "720P", "1080P"],
    },
    duration_ranges: { seedance: [4, 30], kling: [3, 15], minimax: [4, 15], dashscope: [2, 15] },
    model_suggestions: {
      seedance: [
        "doubao-seedance-2-5-260628",
        "doubao-seedance-2-0-260128",
        "doubao-seedance-2-0-fast-260128",
      ],
      kling: ["kling-3.0", "kling-v3-omni"],
      minimax: ["MiniMax-H3", "MiniMax-H3-Max"],
      dashscope: ["wan2.6-t2v", "wan2.6-i2v", "wan2.6-i2v-flash"],
    },
  };
}
