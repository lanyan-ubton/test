// 质量门禁专用 ESLint 配置 (flat config)
// 只含复杂度/体积类硬规则, 与公司项目自身的 eslint 配置互不干扰:
// quality_gate.py 用 --config 显式指定本文件, 不会加载项目配置。
//
// TS/TSX 解析需要项目 node_modules 里有 @typescript-eslint/parser
// (装了 eslint-config-next 的 Next.js 项目通常自带); 找不到时自动只查 js/jsx。

import { createRequire } from "node:module";

const requireFromProject = createRequire(process.cwd() + "/noop.js");

let tsParser = null;
try {
  tsParser = requireFromProject("@typescript-eslint/parser");
} catch {
  console.warn("[quality-gate] 未找到 @typescript-eslint/parser, 本次只检查 js/jsx 文件");
}

const complexityRules = {
  // Bob 给 Agent 的宽松档: 圈复杂度 <= 6 (人类标准是 4)
  "complexity": ["error", { max: 6 }],
  // JSX 会天然撑大行数, 前端放宽到 80; 后端 Python 那边卡 50
  "max-lines-per-function": ["error", { max: 80, skipBlankLines: true, skipComments: true }],
  "max-depth": ["error", 4],
  "max-params": ["error", 4],
  "max-nested-callbacks": ["error", 3],
  // 单文件行数上限, 与后端 --max-file-lines(500) 对齐 (同为总行数口径)
  "max-lines": ["error", { max: 500 }],
};

const ignores = [
  "**/node_modules/**",
  "**/.next/**",
  "**/dist/**",
  "**/build/**",
  "**/*.test.*",
  "**/*.spec.*",
];

const configs = [
  {
    name: "quality-gate/js",
    files: ["**/*.{js,jsx,mjs,cjs}"],
    ignores,
    languageOptions: { ecmaVersion: 2022, sourceType: "module" },
    rules: complexityRules,
  },
];

if (tsParser) {
  configs.push({
    name: "quality-gate/ts",
    files: ["**/*.{ts,tsx,mts,cts}"],
    ignores,
    languageOptions: { parser: tsParser, ecmaVersion: 2022, sourceType: "module" },
    rules: complexityRules,
  });
}

export default configs;
