# 影片知識庫總覽

- 影片數：142
- 分類數：10

# 影片批次整體分析

## 整體描述

這批影片以 **Matt Pocock（totaltypescript.com）** 的內容為主軸，整體圍繞三大主題交織而成：

1. **TypeScript 全棧學習路徑**——從基礎語法、tsconfig 設定、版本演進，一路推進到進階型別技巧（泛型、條件型別、infer、assertion functions 等），再延伸到 React/前端整合、狀態機與函式庫生態（Zod、tRPC、Effect、XState、HOTScript、ArkType、Valibot 等）。
2. **JavaScript 語言與執行環境演進**——涵蓋 WASM、TC39 提案歷史、ESM/CJS 之爭、Node.js 改版等語言本體議題。
3. **AI 輔助開發的新工作流**——以 Claude Code 與 Cursor 為核心，介紹 MCP 協定、Vibe Coding、Skills（/grill-me、/handoff、/prototype、/wayfinder、/teach 等）、規劃模式、TDD 與 AI 結合、context window 與 LLM 基礎知識，並延伸到 Vercel AI SDK 這類實際 AI 應用開發工具。

整體定位偏向「**給資深前端 / TypeScript 開發者的進階內容 + AI 時代的新工具鏈**」，而非純入門教學。

---

## 反覆出現的工具與術語

### 出現在 4 個以上分類的核心詞

| 術語 | 出現分類 |
|---|---|
| **TypeScript** | TypeScript 基礎、進階型別、設定演進、React、架構品質、JS 執行環境、工具生態、AI 工作流（共 8+ 類） |
| **Generics / 泛型** | 進階型別、React、工具生態、JS 執行環境 |
| **MCP（Model Context Protocol）/ MCP server** | AI 工作流、工具生態、LLM 概念 |
| **Claude / Claude Code** | AI 工作流、LLM 概念、架構品質 |
| **Cursor** | AI 工作流、工具生態 |
| **coding agents（AI 編碼代理）** | AI 工作流、LLM 概念 |
| **context window（上下文視窗）/ tokens** | AI 工作流、LLM 概念 |
| **system prompt / user message / assistant message** | AI 工作流、LLM 概念 |
| **Vibe coding / AI 輔助開發** | AI 工作流、工具生態 |
| **Node.js** | JS 執行環境、TypeScript 設定、TypeScript 基礎 |
| **React** | React 前端、進階型別、架構品質 |
| **Zod** | 進階型別、React、工具生態、JS 執行環境 |
| **XState** | 狀態管理、React 前端 |

### 出現在 3 個分類的次要詞

| 術語 | 出現分類 |
|---|---|
| **Plan mode（規劃模式）** | AI 工作流 |
| **Grill Me skill / Grill with Docs** | AI 工作流、LLM 概念 |
| **Handoff skill** | AI 工作流 |
| **Effect** | 工具生態、架構品質 |
| **TDD / Red-Green Refactor / Kent Beck / XP** | 架構品質 |
| **DDD / Ubiquitous Language / Bounded Context** | LLM 概念 |
| **WASM / JavaScript 演進（ES3→ES6）/ TC39** | JS 執行環境 |
| **Vercel AI SDK** | AI 工作流、LLM 概念 |
| **GraphQL / tRPC / monorepo** | 工具生態、React |
| **Claude Haiku 4.5 / Gemini 2.5 Pro** | AI 工作流、LLM 概念 |
| **hallucination（intrinsic / extrinsic）** | LLM 概念 |
| **declare global / interface** | 進階型別、TypeScript 基礎 |
| **const 修飾詞 / 型別參數 / 字面型別** | 進階型別、TypeScript 設定 |
| **ESM / ES Modules** | JS 執行環境、TypeScript 設定 |

---

## 建議觀看順序

### 階段 1：打底（TypeScript / JavaScript 基礎）
1. **TypeScript 基礎與核心語法**——`TypeScript Speedrun`、`Why YOU should migrate to TypeScript`、`Types vs Interfaces`、`Solving the 'any' problem`
   - 理由：後面所有進階內容都預設你已熟悉型別推論、interface 與 type alias 的差異。  
2. **JavaScript 語言與執行環境**——`It FINALLY happened`、`WASM is not ready`、`Node 23 fixes...`、`var, const, let... now 'using'?`、`You're typing process.env wrong`
   - 理由：理解 ESM/CJS、TC39 提案節奏、Node.js 行為，能解開很多 TypeScript 在執行階段的迷惑。

### 階段 2：TypeScript 設定與型別深化
3. **TypeScript 設定與版本演進**——`The TSConfig Cheat Sheet`、`noUncheckedIndexedAccess`、`'.js' files in TypeScript`、`TypeScript 5.5/5.3/5.0`、`Const type parameters`、`erasableSyntaxOnly`
   - 理由：很多團隊連 tsconfig 都不知道怎麼調，遑論運用版本新特性。  
4. **TypeScript 進階型別技巧**——從 `Generics: The most intimidating TypeScript feature`、`KEYOF and EXTENDS`、`as const`、`Using EXTENDS to constrain generics`、`Infer is easier than you think`、`OVERLOADS vs GENERICS`、`CUSTOM ERRORS`、`Master ASSERTION FUNCTIONS` 一路到 `DEEP PARTIAL`、`Dynamic function arguments with GENERICS`
   - 理由：這是 Matt Pocock 的招牌系列，也是看懂後面 Effect/HOTScript/ArkType/Zod+Generics 等影片的門票。

### 階段 3：前端實戰
5. **React 與前端開發**——`Blazing Fast Tips: React & TypeScript`、`Most React devs don't understand generic components`、`Fix your useRefs`、`React Components with GENERICS?`、`Extracting React Props using CONDITIONAL TYPES`、`TypeScript just unblocked React Server Components`、`Should you still be using Redux in 2023?`、`Vercel wants you to fetch data like this`、`Vercel's OG image service`、`Stop using defaultProps`、`Dan Abramov asked me to help fix React`
   - 理由：把型別能力套到實際 React 元件與資料流。  
6. **狀態管理與狀態機**——`Designing State Machines #1~#3`、`XState Docs Speedrun`、`XState - Global state in React`、`Integrating Form Libraries`、`Checking out a new DSL for XState - Lucy!`、`Modelling Statecharts`、`XState Brain Teaser 2`
   - 理由：狀態機思維是 Redux 之後下一代狀態管理的核心，學完可直接套到複雜表單與流程。

### 階段 4：程式碼品質與架構
7. **程式碼架構與品質**——`Should you use return types?`、`This saved me HOURS of refactoring time`、`This TS naming convention is weird`、`Your codebase is NOT ready for AI`、`Red Green Refactor is OP With Claude Code`、`Can Cursor's HARDCORE Review Skill Stop The Slop?`、`How To De-Slop A Codebase Ruined By AI`、`My "as few deps as possible" monorepo setup`
   - 理由：把 TDD/XP 與 AI 工具結合，是這批內容最能直接落地到日常工作的環節。

### 階段 5：TypeScript 生態系工具
8. **TypeScript 工具與函式庫生態**——`Build an Express App with Sextant`、`Everyone's talking about gql.tada`、`Everyone's talking about HOTScript`、`Everyone's talking about ArkType`、`Everyone's talking about Valibot`、`Preconstruct`、`Blazing Fast Tips: Publishing to NPM`、`Learn tRPC in 5 minutes`、`The TS Playground has PLUGINS`、`Effect: the unreadable library`、`A Complete Guide To Vercel's AI SDK`、`Zod 4 is out`、`Zod + Generics is HEAVEN`、`The best TypeScript library just got better`、`Automate your life with TypeScript`、`My favourite VSCode extension has 3k downloads`、`Does ts-reset fix TypeScript?`、`Codegen`、`I Open-Sourced My Own AFK Software Factory`、`I quit Vercel to make content`
   - 理由：這些是「選型決策」內容，看完前三階段再看才能判斷該不該引入 Effect / ArkType / HOTScript。

### 階段 6：AI 與 LLM 基礎
9. **LLM 與 AI 開發概念**——`Most devs don't understand how context windows work`、`Most devs don't understand how LLM tokens work`、`Most devs don't understand what agents are`、`Never Trust An LLM`
   - 理由：這四支是**所有 AI 工作流影片的前置知識**，建議排在 Skills/Claude Code 內容之前看，否則會看不懂為什麼要這樣設計 skill。

### 階段 7：AI 編碼代理與 Skills 工作流
10. **AI 編碼代理與 Skills 工作流**——從 `Get Started With The Model Context Protocol // 2-Minute Tutorial`、`Build An MCP Server In 5 Prompts` 入門，再看 `I was an AI skeptic. Then I tried plan mode`、`AI SDK 6 is SWEET`、`How I use Claude Code for real engineering`、`Building a REAL feature with Claude Code: every step explained`、`The 7 phases of AI-driven development`、`Ship working code while you sleep with the Ralph Wiggum technique`，接著進到 Skills 家族：`Learn anything with the /teach skill`、`5 Claude Code skills I use every single day`、`9 Things People Get Wrong With My /grill-* skills`、`I stopped using /grill-me for coding. Here's what I use instead`、`/handoff is my new favourite skill`、`/wayfinder: Nothing is too big to plan anymore`、`Don't waste time on specs: /prototype instead`、`Burn through the backlog from hell with /triage`、`New Skills! v1.1` 與 `New Skills! /handoff, /prototype, /review`、`Red Green Refactor is OP`、`Never Run claude /init`、`Claude Code tried to improve /init...`、`How to actually force Claude Code to use the right CLI`、`I'm using claude --worktree for everything now`、`mattpocock/skills: A complete AI Coding workflow, end-to-end`、`Anthropic's "dedicated monthly credit" is actually a huge cut`、`Frontend is HARDER for AI than backend (here's how to fix it)`
    - 理由：這是全系列最龐大的主題，放最後才能融會貫通 LLM 知識、TypeScript 能力、狀態機思維與 TDD 紀律。

---

## 涵蓋落差

這批影片雖然涵蓋面廣，但仍明顯缺少以下主題——這些都是「一個完整的 TypeScript / 前端 / AI 開發者學習路徑」應該要有的內容：

### 1. 測試生態系（僅觸及 TDD 概念，沒有實戰）
- 沒有 **Vitest / Jest** 的深入教學
- 沒有 **Playwright / Cypress** 等 E2E 測試
- 沒有 **Mock / Stub / Snapshot** 策略
- 沒有 **測試覆蓋率工具**（Istanbul / c8）

### 2. 後端與資料層
- **資料庫 / ORM** 完全缺席（Drizzle、Prisma、Kysely 等都沒提到）
- **Express 影片僅一支入門**，沒有涵蓋 middleware 設計、錯誤處理、認證（OAuth、JWT）
- 沒有 **RESTful API 設計原則**、API versioning、rate limiting
- 沒有 **背景任務 / Queue**（BullMQ、Inngest 等）

### 3. 部署與基礎設施
- **CI/CD**（GitHub Actions、build pipeline）幾乎沒有
- **Docker / 容器化**缺席
- **監控與日誌**（Sentry、Datadog、OpenTelemetry）缺席
- **Vercel 之外的部署選項**（Cloudflare Workers、Fly.io、Railway）僅在 AI SDK 影片中順帶提及

### 4. 效能與 Web 標準
- 雖然談到 WASM 與 WebGL（Zaplib），但缺少**前端效能優化系統性內容**（Core Web Vitals、bundle 分析、code splitting 策略）
- 沒有 **CSS / Tailwind / 設計系統**相關影片
- 沒有 **瀏覽器 API**（Fetch、Service Worker、PWA、IndexedDB）
- 沒有 **a11y / 國際化**主題

### 5. 安全
- 沒有 **XSS / CSRF / CSP** 等前端安全專題
- 沒有 **依賴安全**（npm audit、SBOM、socket.dev）
- 沒有 **密鑰管理、secrets rotation**

### 6. AI 內容的明顯缺口
- **RAG 與向量資料庫**（雖然 AI SDK 有，但 Pinecone / pgvector / embeddings 流程沒系統講）
- **Fine-tuning 與 LoRA** 等模型自訂技術
- **Evals / 評測**——如何系統性評估 LLM 應用品質
- **多代理 / 多模型編排**（multi-agent orchestration）的設計模式
- **成本與 token 預算管理**（僅一支影片順帶提到）
- **function calling / tool use** 的內部機制深入解釋

### 7. 團隊與流程
- 沒有 **Code Review 流程**（雖然 Cursor 有一支 review skill 影片，但屬 AI 工具面）
- 沒有 **Monorepo 工具鏈比較**（Turborepo、Nx、Rush），僅一支「as few deps as possible」影片
- 沒有 **套件發布與維護**（版本號、changelog、semver）的完整流程
- 沒有 **文件與 API 文件**生成策略（雖然 Codegen 有，但偏 GraphQL）

### 8. 其他生態主流工具
- 完全沒有 **Vue / Svelte / Solid** 的深入內容（XState 影片有順帶提到，但獨立深度系列沒有）
- 沒有 **Node.js 以外的後端 runtime**（Bun、Deno）效能比較
- 沒有 **VS Code 以外的編輯器**（Neovim、Zed、WebStorm）對 TypeScript / AI 的支援

## 主題分群

### TypeScript 基礎與核心語法（16 支）

- [TypeScript Speedrun: Crash Course for Beginners](https://www.youtube.com/watch?v=YmxwicpROps) · 23.6 分 — 本影片是一場 TypeScript 官方 handbook 初學者章節的「speedrun」導覽，由講者帶領觀眾逐步走過 TypeScrip
- [6 TypeScript tips to turn you into a WIZARD](https://www.youtube.com/watch?v=lraHlXpuhKs) · 15.9 分 — 本影片是一支 TypeScript 技巧合輯，依難度由淺入深整理了數個實用技巧：key optional 與 value optional 
- [8 TypeScript Tips To Expand Your Mind (and improve your code)](https://www.youtube.com/watch?v=QSIXYMIJkQg) · 10.9 分 — 影片統整作者一月份在 Twitter、LinkedIn 與 Instagram 上發布的一系列 TypeScript 小技巧，依序涵蓋 Re
- [Types vs Interfaces: What I Got Wrong In 2022](https://www.youtube.com/watch?v=e0AIkYrXAYE) · 9.9 分 — 影片作者回顧自己 2022 年關於 TypeScript「type vs interface」的舊影片，逐一更正錯誤並提出新看法
- [Enums considered harmful](https://www.youtube.com/watch?v=jjMbPt_H3RQ) · 9.4 分 — 本影片探討 TypeScript 中 enum 的使用爭議
- [Why YOU should migrate to TypeScript](https://www.youtube.com/watch?v=dg1eJZk7waw) · 7.1 分 — Matt Pocock（totaltypescript.com 作者）說明大家紛紛轉用 TypeScript 的六大主因：在 IDE 即時顯
- [Blazing Fast Tips: React & TypeScript](https://www.youtube.com/watch?v=37PafxU_uzQ) · 6.2 分 — 本影片介紹如何在 React（搭配 Next.js 專案）中有效使用 TypeScript
- [Solving the 'any' problem is harder than you think](https://www.youtube.com/watch?v=ExhQxFapKCs) · 5.1 分 — 本影片以實作 groupBy 工具函式為例，探討 TypeScript 中 any、unknown 與泛型（generics）三種型別處理方
- [Should you use return types?](https://www.youtube.com/watch?v=nwSe95uFN8E) · 4.7 分 — 本影片介紹 TypeScript 的函式回傳型別（return types），說明簡單函式可依賴型別推論、不必顯式標註；但含有多重分支（如 
- [[DEPRECATED] TypeScript: Should you use Types or Interfaces?](https://www.youtube.com/watch?v=zM9UPcIyyhQ) · 4.1 分 — 影片講述講者對 TypeScript 中 type 與 interface 的看法經歷三個階段的轉變：從偏好 interface（認為效能較
- [This TS naming convention is weird](https://www.youtube.com/watch?v=qA65QjWCl60) · 4.0 分 — 本影片由 Matt Pocock 主講，介紹 TypeScript 中命名型別（type）的四個實用規則：型別名稱一律使用單數（陣列除外）、
- [Don't put your types in .d.ts files](https://www.youtube.com/watch?v=zu-EgnbmcLY) · 3.9 分 — 影片說明 TypeScript 中兩種型別宣告方式的差異——正常的 .ts/.tsx 模組與 .d.ts 宣告檔，並指出常見的反模式：開發者
- [This launch took 11 MONTHS of my life](https://www.youtube.com/watch?v=wgcfq51twe0) · 3.8 分 — 影片創作者宣布四項重大消息：免費推出的 TypeScript 電子書《Total TypeScript Essentials》（共 16 章
- ['.js' files in TypeScript - why?!](https://www.youtube.com/watch?v=8ORIzvgNWhU) · 3.2 分 — 本影片說明 TypeScript 為何改變 JavaScript / Node.js 環境中撰寫 import 路徑的方式
- [TypeScript's Utility Types... Blazing fast](https://www.youtube.com/watch?v=EU0TB_8KHpY) · 3.1 分 — 本影片介紹 TypeScript 內建的 Utility Types，包含物件類型（Partial、Required、Omit、Pick、R
- [Will this code error?](https://www.youtube.com/watch?v=VLyY9UC9W80) · 1.9 分 — 影片以「Will It Error」小遊戲開場，展示三個分別接收 number、string、Boolean 的函式，並將其組成的陣列以 m

### TypeScript 進階型別技巧（52 支）

- [Generics: The most intimidating TypeScript feature](https://www.youtube.com/watch?v=dLPgQRbVquo) · 18.3 分 — 影片介紹 10 個讓你成為 TypeScript 泛型（generics）高手的技巧，內容涵蓋泛型型別、泛型函式、型別推論、泛型約束、多重泛
- [6 TypeScript tips to turn you into a WIZARD](https://www.youtube.com/watch?v=lraHlXpuhKs) · 15.9 分 — 本影片是一支 TypeScript 技巧合輯，依難度由淺入深整理了數個實用技巧：key optional 與 value optional 
- [Infer is easier than you think](https://www.youtube.com/watch?v=hLZXJTm7TEk) · 13.6 分 — 本影片以 TypeScript 的 `infer` 關鍵字為主題，從條件型別（Conditional Types）的基本概念出發，逐步拆解內
- [Zod 4 is out, and it is CRAZY](https://www.youtube.com/watch?v=xcm53k0ePmY) · 11.2 分 — 影片介紹 Zod 4 beta 版本的新功能與改進，涵蓋執行效能提升、TypeScript 型別效能優化、bundle size 縮減、Zo
- [8 TypeScript Tips To Expand Your Mind (and improve your code)](https://www.youtube.com/watch?v=QSIXYMIJkQg) · 10.9 分 — 影片統整作者一月份在 Twitter、LinkedIn 與 Instagram 上發布的一系列 TypeScript 小技巧，依序涵蓋 Re
- [TypeScript 4.9 deep dive](https://www.youtube.com/watch?v=Danki1DyiuI) · 10.1 分 — 本影片介紹 TypeScript 4.9 版本的主要新功能與改進，包括備受矚目的 satisfies 運算子、針對未列屬性（unlisted
- [Our first look at INFER - Type Challenges #4](https://www.youtube.com/watch?v=dZWJrjzqvv8) · 8.6 分 — 本影片講解如何用 TypeScript 實作一個泛型 `First<T>` 型別，能取得陣列（或元組）的第一個元素型別
- [Transforming a TUPLE to an OBJECT - Type Challenges #3](https://www.youtube.com/watch?v=nK6qW_NsPvc) · 7.5 分 — 本影片為 TypeScript 類型挑戰教學，目標是實作一個 TupleToObject 泛型類型，將給定的陣列（tuple）轉換成以陣列元
- [5 Things They’ll NEVER Add To TypeScript](https://www.youtube.com/watch?v=Zsj1UlCsuio) · 7.3 分 — 本影片由 Matt Pocock 主講，介紹 TypeScript 官方承諾永不實作的五大熱門特性：寬鬆設計的 Omit、否定型別（nega
- [The best TypeScript library just got better](https://www.youtube.com/watch?v=CughWjaoXZw) · 6.6 分 — 影片介紹 Zod 3.20（講者一開始口誤為 3.2）的新功能，包含 `.pipe()` 串接驗證管線、`z.coerce` 型別強制轉換、
- [KEYOF and EXTENDS right off the bat?! - Type Challenges #1](https://www.youtube.com/watch?v=knOgB-Aa8Pc) · 6.1 分 — 本影片是 TypeScript 型別挑戰系列的開篇，作者挑戰不使用內建 Pick泛型，從零實作出功能等價的 Pick<T, K>
- [Most React devs don’t understand generic components](https://www.youtube.com/watch?v=zqz-lEHjJr4) · 5.7 分 — 本影片介紹如何在 React 搭配 TypeScript 的情境下建立泛用（generic）元件，以 Table 元件為例，說明如何透過型別
- [as const: the most underrated TypeScript feature](https://www.youtube.com/watch?v=6M9aZzm-kEc) · 5.6 分 — 影片介紹 TypeScript 中最被低估的功能——`as const`，透過一個路由物件的範例，展示 `as const` 如何將屬性型別
- [Solving the 'any' problem is harder than you think](https://www.youtube.com/watch?v=ExhQxFapKCs) · 5.1 分 — 本影片以實作 groupBy 工具函式為例，探討 TypeScript 中 any、unknown 與泛型（generics）三種型別處理方
- [Is "+readonly" really legal?! - Type Challenges #2](https://www.youtube.com/watch?v=U1EygIpjAEM) · 5.0 分 — 本影片為 TypeScript 型別挑戰，目標是手刻內建 Readonly 工具型別，讓物件的最外層屬性全部變為唯讀
- [The empty object is TypeScript's weirdest type](https://www.youtube.com/watch?v=i4-QixSfCpY) · 5.0 分 — 本影片深入探討 TypeScript 中被稱為「最奇怪的型別」的空物件型別（empty object type）
- [How is this a thing?! - Type Challenges #5](https://www.youtube.com/watch?v=iNs_z-vsfVM) · 4.8 分 — 這是一段 TypeScript 教學影片，講解 Type Challenges 中的「Length of Tuple」挑戰（編號 5）
- [Zod + Generics is HEAVEN](https://www.youtube.com/watch?v=9N50YV5NHaE) · 4.7 分 — 本影片介紹如何將 Zod 與 TypeScript 泛型結合使用
- [Most TS devs don't understand 'satisfies'](https://www.youtube.com/watch?v=r1L35zxZQPE) · 4.2 分 — 影片介紹 TypeScript 中 `satisfies` 運算子與變數註解（variable annotation）的差異
- [Const type parameters are so powerful](https://www.youtube.com/watch?v=hk6ZkD0Vg3w) · 3.9 分 — 介紹 TypeScript 5.0 即將推出的一項新功能——型別參數的 const 修飾詞
- [Dan Abramov asked me to help fix React](https://www.youtube.com/watch?v=M3TPmOpNFCs) · 3.9 分 — 影片作者分享自己在 Twitter 上向 Dan Abramov 指出 TypeScript 與 React 的型別問題，獲得 Dan 邀請
- [Declaring globals isn't that hard](https://www.youtube.com/watch?v=0zp9bkEfaOc) · 3.8 分 — 本影片介紹 TypeScript 開發者常犯的錯誤：為全域物件（globals）宣告型別時使用 `window as any`
- [Rethink your life choices before you do this...](https://www.youtube.com/watch?v=uiOJ5_7cB4I) · 3.7 分 — 本影片說明如何使用 TypeScript 的區辨聯合（discriminated union）搭配 `as` prop，建構一個同時支援 b
- [Yes, we CAN make Object.assign type-safe...](https://www.youtube.com/watch?v=jEeQC6I8nlY) · 3.5 分 — 影片示範如何使用 TypeScript 的型別魔法，為一個使用 Object.assign 合併物件陣列的函式正確標註型別
- [You're typing process.env wrong](https://www.youtube.com/watch?v=q1im-hMlKhM) · 3.4 分 — 影片介紹如何利用 Zod 將 Node.js 應用程式的環境變數做型別化處理
- [Everyone's talking about HOTScript](https://www.youtube.com/watch?v=QDANnOSMCOw) · 3.3 分 — 影片介紹 TypeScript 函式庫「Hot Script」，展示其高階型別（higher-order types）概念
- [TypeScript's Utility Types... Blazing fast](https://www.youtube.com/watch?v=EU0TB_8KHpY) · 3.1 分 — 本影片介紹 TypeScript 內建的 Utility Types，包含物件類型（Partial、Required、Omit、Pick、R
- [Dynamic objects should NOT be this hard](https://www.youtube.com/watch?v=Gxd4StCrN3g) · 3.1 分 — 本影片講解 TypeScript 中如何以動態（字串）索引存取物件屬性，並提出三種解法：使用 `keyof typeof` 收緊索引型別、改
- [Everyone's talking about ArkType](https://www.youtube.com/watch?v=-_0wMxtRVqA) · 2.8 分 — Matt 介紹一個名為 Archetype 的 TypeScript 型別驗證函式庫，可視為 Zod 的替代方案
- [Decode search params with TYPESCRIPT ONLY - Advanced TypeScript](https://www.youtube.com/watch?v=8gDpiILuf2I) · 2.3 分 — 本影片介紹 TypeScript 字串插值（模板字面值型別）的進階用法，示範如何將類似查詢字串的輸入（如 a=wonderful&b=wow
- [WHEN should I use a Generic? - Advanced TypeScript](https://www.youtube.com/watch?v=lMfGp29Ht8c) · 2.3 分 — 本段影片講解 TypeScript 何時該使用泛型（Generic）
- [Master FUNCTION OVERLOADS with 'compose' - Advanced TypeScript](https://www.youtube.com/watch?v=D1a8OoBWi1g) · 2.3 分 — 本影片介紹 TypeScript 中的函式重載（function overloads），以一個 `compose` 函式為例，展示如何透過多
- [Using DECLARE GLOBAL for amazing inference - Advanced TypeScript](https://www.youtube.com/watch?v=byCYSXVmH6E) · 2.2 分 — 本影片介紹如何在 TypeScript 中使用全域 (globals) 來為多個 reducer 定義共用的事件型別
- [Turn MODULES INTO TYPES with typeof import - Advanced TypeScript](https://www.youtube.com/watch?v=sswUBXaoXSI) · 2.1 分 — 本影片介紹如何使用 TypeScript 的 `typeof import` 語法，從 `constants.ts` 動態推導出模組型別，再
- [Fix your useRefs with ElementRef](https://www.youtube.com/watch?v=JuULWGuoI1A) · 2.1 分 — 本影片介紹 React 中的 ElementRef 型別，說明如何利用它為 useRef 提供強型別，取代手動從 lib.dom.d.ts 
- [Master Generic Slots in TypeScript - Advanced TypeScript](https://www.youtube.com/watch?v=-rJ2XzQg8Ek) · 2.0 分 — 講解 TypeScript 中泛型（generics）與函式執行環境（execution context）的關聯，並以 `makeKeyRe
- [Literal types are SO USEFUL in TS - Advanced TypeScript](https://www.youtube.com/watch?v=jxSh-RZTCws) · 2.0 分 — 本影片說明 TypeScript 編譯器在型別推論上的行為差異，特別是 `let`、`const` 與 `as const` 對基本型別、陣
- [DRY up your generic signatures with DEFAULT GENERICS - Advanced TypeScript](https://www.youtube.com/watch?v=z7thLMrTrZs) · 2.0 分 — 影片說明如何利用泛型的預設型別參數（default type parameters of generics），將複雜泛型簽章中重複的型別運算
- [REMOVE a member of a Union Type - Advanced TypeScript](https://www.youtube.com/watch?v=M4-Jl9JWGmo) · 1.9 分 — 本影片透過一個「如何從聯合型別中移除一個成員」的小謎題，介紹 TypeScript 的分配性條件型別（Distributive Condit
- [Dynamic function arguments with GENERICS - Advanced TypeScript](https://www.youtube.com/watch?v=YE_3WwX-Dl8) · 1.9 分 — 本影片說明 TypeScript 中函式引數需依情況為選擇性的問題
- [DEEP PARTIAL in TypeScript - Advanced TypeScript](https://www.youtube.com/watch?v=AhzjPAtzGTs) · 1.9 分 — 本影片介紹 TypeScript 中 Deep Partial 工具型別的使用情境與實作原理
- [Will this code error?](https://www.youtube.com/watch?v=VLyY9UC9W80) · 1.9 分 — 影片以「Will It Error」小遊戲開場，展示三個分別接收 number、string、Boolean 的函式，並將其組成的陣列以 m
- [Extracting React Props using CONDITIONAL TYPES - Advanced TypeScript](https://www.youtube.com/watch?v=VBsKNKEZNnY) · 1.8 分 — 本影片介紹 TypeScript 中一個常見的模式：建立型別輔助函式（type helper）來從非型別的值中萃取型別
- [This saved me HOURS of refactoring time](https://www.youtube.com/watch?v=6l3Q9DjbQ7A) · 1.8 分 — 這段影片介紹一個 TypeScript 重構小技巧：當需要為部分使用者加上 `role` 屬性時，可以先在函數簽章中將屬性設為「可傳入 `u
- [Master ASSERTION FUNCTIONS in TypeScript - Advanced TypeScript](https://www.youtube.com/watch?v=8lM609lci7E) · 1.8 分 — 本段影片介紹 TypeScript assertion functions 套用於 class 的做法
- [Fix ANNOYING OBJECT KEYS with infer and template literals](https://www.youtube.com/watch?v=PEQn1a6xOHc) · 1.7 分 — 本影片說明如何透過 TypeScript 型別輔助函式，從 API 回傳的物件中移除所有屬性鍵名上的「maps」前綴，並介紹如何利用條件型別
- [OVERLOADS vs GENERICS: which is better? - Advanced TypeScript](https://www.youtube.com/watch?v=Vr1BUFw6dJM) · 1.4 分 — 本影片以一個名為 return-what-i-pass-in 的函式為例，說明 TypeScript 中函式多載（function over
- [React Components with GENERICS? - Advanced TypeScript](https://www.youtube.com/watch?v=vCq9QsDinvo) · 1.4 分 — 本影片說明如何在 React 元件中使用 TypeScript 泛型（generics），讓元件能接收任意型別的 items 並將型別傳遞給
- [CUSTOM ERRORS in TypeScript? - Advanced TypeScript](https://www.youtube.com/watch?v=7BnoNWu2y3Y) · 1.3 分 — 本段影片示範如何將比較邏輯從 runtime 移至 TypeScript 的型別層級，以 deep equal compare 函式為例，說
- [The SECRET POWER of indexed access types - Advanced TypeScript](https://www.youtube.com/watch?v=plsnFfbqVEo) · 1.3 分 — 本影片介紹 TypeScript 的 indexed access types（索引存取型別），示範如何透過型別運算存取物件、陣列與元組中較
- [Using EXTENDS to constrain generics - Advanced TypeScript](https://www.youtube.com/watch?v=xZ8BBBdMwQI) · 1.1 分 — 本影片介紹 TypeScript 泛型中如何使用 extends 關鍵字來縮小泛型的範圍，使其更具實用性
- [Improving OBJECT.KEYS in TypeScript - Advanced TypeScript](https://www.youtube.com/watch?v=GW00zebIt0g) · 1.1 分 — 影片說明 TypeScript 中使用內建 Object.keys 時，回傳的鍵會被推論為 string 而非物件實際鍵的聯合型別，導致後續

### TypeScript 設定與版本演進（16 支）

- [TypeScript 4.9 deep dive](https://www.youtube.com/watch?v=Danki1DyiuI) · 10.1 分 — 本影片介紹 TypeScript 4.9 版本的主要新功能與改進，包括備受矚目的 satisfies 運算子、針對未列屬性（unlisted
- [TypeScript 5.5 is a BANGER](https://www.youtube.com/watch?v=sciBO_IaxTw) · 9.2 分 — 影片介紹 TypeScript 5.5 的多項新功能，包括正規表達式語法檢查、monorepo 設定檔模板變數 `${configDir}`
- [Why YOU should migrate to TypeScript](https://www.youtube.com/watch?v=dg1eJZk7waw) · 7.1 分 — Matt Pocock（totaltypescript.com 作者）說明大家紛紛轉用 TypeScript 的六大主因：在 IDE 即時顯
- [TypeScript 5.0 BETA IS HERE](https://www.youtube.com/watch?v=iOTAFRFgm8I) · 6.7 分 — 本影片介紹 TypeScript 5.0 beta 版的新功能
- [TypeScript 5.3 First Look](https://www.youtube.com/watch?v=h5ymIiux6NU) · 6.4 分 — 影片介紹尚未正式發布的 TypeScript 5.3 迭代計畫，內容涵蓋 import attributes、throw expressio
- [The TSConfig Cheat Sheet](https://www.youtube.com/watch?v=eJXVEju3XLM) · 5.6 分 — 本影片針對 TypeScript 的 tsconfig.json 設定檔，提供一份速查攻略，整理出在大多數情境下「幾乎都該設」的基礎選項（b
- [var, const, let... now ‘using’?](https://www.youtube.com/watch?v=VqdVaix3Zvw) · 4.4 分 — 本影片介紹即將加入 JavaScript 與 TypeScript 的新關鍵字「using」，它能用於宣告具有自動釋放（dispose）機制
- [TypeScript 5.0 is coming...](https://www.youtube.com/watch?v=PTU-H6Wwbh8) · 4.2 分 — 本影片介紹 TypeScript 5.0 的預定時程與重點新功能
- [TypeScript 5.3 is a no-brainer upgrade](https://www.youtube.com/watch?v=-SM-xZLMj-g) · 4.2 分 — 本影片介紹 TypeScript 5.3 的三項主要更新：Import Attributes（匯入屬性）、switch(true) 的型別窄
- [Const type parameters are so powerful](https://www.youtube.com/watch?v=hk6ZkD0Vg3w) · 3.9 分 — 介紹 TypeScript 5.0 即將推出的一項新功能——型別參數的 const 修飾詞
- [Don't put your types in .d.ts files](https://www.youtube.com/watch?v=zu-EgnbmcLY) · 3.9 分 — 影片說明 TypeScript 中兩種型別宣告方式的差異——正常的 .ts/.tsx 模組與 .d.ts 宣告檔，並指出常見的反模式：開發者
- ['.js' files in TypeScript - why?!](https://www.youtube.com/watch?v=8ORIzvgNWhU) · 3.2 分 — 本影片說明 TypeScript 為何改變 JavaScript / Node.js 環境中撰寫 import 路徑的方式
- [TypeScript just unblocked React Server Components](https://www.youtube.com/watch?v=DTYNJjKlzFs) · 2.8 分 — 影片介紹 TypeScript 5.1 beta 版的兩大重點更新：第一是放寬對 undefined 返回函式的型別推斷與檢查；第二是解耦 
- [It FINALLY happened](https://www.youtube.com/watch?v=0uGkjjKLCsU) · 2.7 分 — 影片介紹 Node.js 23 正式內建（unflag）實驗性的 TypeScript 支援，無需額外設定即可直接執行 .ts 檔案
- [TypeScript shipped a flag to disable enums](https://www.youtube.com/watch?v=zeNh4fuJhcA) · 2.5 分 — 影片介紹 TypeScript 5.8 新加入的編譯選項 erasableSyntaxOnly，此旗標可關閉 enums、namespace
- ["noUncheckedIndexedAccess" - the BEST config option you've never heard of](https://www.youtube.com/watch?v=JwZaP3pY7Zo) · 1.8 分 — 本影片介紹 TypeScript 中 `noUncheckedIndexedAccess` 編譯器選項的用途與效果

### TypeScript 工具與函式庫生態（23 支）

- [A Complete Guide To Vercel’s AI SDK // The ESSENTIAL Tool For Shipping AI Apps](https://www.youtube.com/watch?v=mojZpktAiYQ) · 38.4 分 — 本影片是 Vercel AI SDK 的完整教學，從基礎的文字生成（generateText）、串流（streamText）、系統提示詞、多
- [Effect: the unreadable library that captured my heart](https://www.youtube.com/watch?v=S2GChOwivwQ) · 13.1 分 — 影片由開發者分享使用 Effect（TypeScript 函式庫）約 18 個月後的心得
- [Build An MCP Server In 5 Prompts // Vibe Coding That Doesn't Suck](https://www.youtube.com/watch?v=FRogt98OF80) · 12.7 分 — 本影片示範如何在五個提示詞（prompt）的輔助下，從空目錄開始建構一個 TypeScript MCP（Model Context Prot
- [I Open-Sourced My Own AFK Software Factory](https://www.youtube.com/watch?v=E5-QK3CDVQM) · 11.4 分 — 本影片介紹作者自製的 TypeScript 函式庫 Sandcastle，用於在隔離沙箱中編排 AI 編碼代理人（如 Claude Code
- [Zod 4 is out, and it is CRAZY](https://www.youtube.com/watch?v=xcm53k0ePmY) · 11.2 分 — 影片介紹 Zod 4 beta 版本的新功能與改進，涵蓋執行效能提升、TypeScript 型別效能優化、bundle size 縮減、Zo
- [Codegen: The good, the bad and the ugly](https://www.youtube.com/watch?v=5weFyMoBGN4) · 6.6 分 — 本影片介紹程式碼生成（codegen）工具在 TypeScript 專案中的用途，特別以 GraphQL Codegen 為例
- [The best TypeScript library just got better](https://www.youtube.com/watch?v=CughWjaoXZw) · 6.6 分 — 影片介紹 Zod 3.20（講者一開始口誤為 3.2）的新功能，包含 `.pipe()` 串接驗證管線、`z.coerce` 型別強制轉換、
- [Learn tRPC in 5 minutes](https://www.youtube.com/watch?v=S6rcrkbsDI0) · 6.0 分 — 本影片為 tRPC 的快速入門教學，介紹 tRPC 作為一個使用 TypeScript 串接前後端、提供型別安全通訊的全端框架
- [I quit Vercel to make content](https://www.youtube.com/watch?v=l2iVQkY6mXQ) · 5.4 分 — 創作者回顧 2022 年的職涯歷程：從 1 月的 4,000 名 Twitter 追�者起步，2 月靠一支 2 分鐘的 TypeScript
- [Everyone's talking about gql.tada](https://www.youtube.com/watch?v=PzhxN1u6ziQ) · 5.1 分 — 介紹一個 TypeScript 的 GraphQL 查詢引擎函式庫 gql.tada，搭配 graphql-sp 語言伺服器外掛，能在編輯 
- [Zod + Generics is HEAVEN](https://www.youtube.com/watch?v=9N50YV5NHaE) · 4.7 分 — 本影片介紹如何將 Zod 與 TypeScript 泛型結合使用
- [Everyone's talking about Valibot](https://www.youtube.com/watch?v=795SKl70JK8) · 4.7 分 — 本影片為直播片段，主講者介紹 Valibot 這個新興的 TypeScript schema 驗證函式庫，並將其與 Zod 進行比較
- [Does ts-reset fix TypeScript?](https://www.youtube.com/watch?v=bUts4HJJjV4) · 4.5 分 — 影片介紹作者發布的 TypeScript 函式庫「TS Reset」(typescript-reset)，說明它如何修正 TypeScrip
- [My "as few deps as possible" monorepo setup](https://www.youtube.com/watch?v=hRyU0bN7qhw) · 4.0 分 — 介紹如何以最少依賴（pnpm、TypeScript、Turborepo）建立 monorepo，涵蓋目錄結構、turbo build 與 t
- [Blazing Fast Tips: Publishing to NPM](https://www.youtube.com/watch?v=eh89VE3Mk5g) · 3.6 分 — 本影片教學如何運用現代工具鏈（pnpm、TypeScript、TSUp、Changesets 與 GitHub Actions）將一個套件從
- [Automate your life with TypeScript](https://www.youtube.com/watch?v=C14MSrJGGBg) · 3.5 分 — 本影片介紹一款名為 Script Kit 的開源工具，它是一個以 TypeScript 撰寫腳本的指令面板（command palette）
- [Everyone's talking about HOTScript](https://www.youtube.com/watch?v=QDANnOSMCOw) · 3.3 分 — 影片介紹 TypeScript 函式庫「Hot Script」，展示其高階型別（higher-order types）概念
- [Build an Express App with Sextant #1 - Getting Started](https://www.youtube.com/watch?v=EEShpJ780aU) · 3.1 分 — 本影片介紹如何使用 Sextant 插件輔助 Express 應用開發
- [Everyone's talking about ArkType](https://www.youtube.com/watch?v=-_0wMxtRVqA) · 2.8 分 — Matt 介紹一個名為 Archetype 的 TypeScript 型別驗證函式庫，可視為 Zod 的替代方案
- [The TS Playground has PLUGINS](https://www.youtube.com/watch?v=PHcm4Hwot2c) · 2.7 分 — Matt Pocock 分享他長期以來錯誤使用 TypeScript Playground 的經驗，介紹三個實用插件：Prettier 格式
- [Get Started With The Model Context Protocol // 2-Minute Tutorial](https://www.youtube.com/watch?v=MC2BwMGFRx4) · 2.4 分 — 本影片示範如何以單一 TypeScript 檔案（無需建置步驟）建立 MCP server，並透過 NPX 與 TSX 將其註冊到 Clau
- [My favourite VSCode extension has 3k downloads](https://www.youtube.com/watch?v=u0adKDu--cA) · 2.2 分 — 本影片介紹一款 VS Code 擴充功能，可將 TypeScript Playground 中用於顯示型別的註解語法帶進編輯器，讓開發者直接
- [Preconstruct is AMAZING for bundling TS libraries - Advanced TypeScript](https://www.youtube.com/watch?v=sZ6vkIQMwT8) · 1.8 分 — 本影片介紹如何使用 preconstruct CLI 工具快速將 TypeScript 套件建置並發布到 NPM

### JavaScript 語言與執行環境（10 支）

- [Arrays, objects... now ‘composites’?](https://www.youtube.com/watch?v=hFenspfGLTk) · 9.3 分 — 本影片介紹 JavaScript 中 Records & Tuples 提案被撤回的事件，並說明取而代之的 Composites 提案如何嘗
- [They nearly gave JavaScript types](https://www.youtube.com/watch?v=hPholNWkc7s) · 4.9 分 — 本影片介紹 JavaScript 歷史上從未正式發布的 ES4 版本
- [var, const, let... now ‘using’?](https://www.youtube.com/watch?v=VqdVaix3Zvw) · 4.4 分 — 本影片介紹即將加入 JavaScript 與 TypeScript 的新關鍵字「using」，它能用於宣告具有自動釋放（dispose）機制
- [Node 23 fixes the worst thing about JavaScript](https://www.youtube.com/watch?v=RV01eSRO_Zk) · 4.3 分 — 本影片介紹 Node.js 23（實驗版本）如何解決 CommonJS（CJS）無法直接 require ESM 模組的經典錯誤
- [Import vs Require: The Biggest JavaScript Divide](https://www.youtube.com/watch?v=6_JNPmjSevo) · 4.0 分 — 本影片探討 JavaScript 社群長期爭論的模組系統議題：CommonJS 與 ES Modules（ESM）的差異、起源、相容性問題，
- [WASM is not ready](https://www.youtube.com/watch?v=PHvT3QShPTo) · 3.7 分 — 影片引用 Zaplib 公司的 post-mortem 文章，探討「Rust 與 WASM 是否比 JavaScript 快」這個常見論點
- [Automate your life with TypeScript](https://www.youtube.com/watch?v=C14MSrJGGBg) · 3.5 分 — 本影片介紹一款名為 Script Kit 的開源工具，它是一個以 TypeScript 撰寫腳本的指令面板（command palette）
- [You're typing process.env wrong](https://www.youtube.com/watch?v=q1im-hMlKhM) · 3.4 分 — 影片介紹如何利用 Zod 將 Node.js 應用程式的環境變數做型別化處理
- [It FINALLY happened](https://www.youtube.com/watch?v=0uGkjjKLCsU) · 2.7 分 — 影片介紹 Node.js 23 正式內建（unflag）實驗性的 TypeScript 支援，無需額外設定即可直接執行 .ts 檔案
- [Going weirdly deep on console.log](https://www.youtube.com/watch?v=Ozg5UqaD5fg) · 1.2 分 — 本影片介紹在 Node.js 中處理深層巢狀物件的 console 輸出技巧，從 console.log 預設會在中途截斷成 [Object

### AI 編碼代理與 Skills 工作流（32 支）

- [Building a REAL feature with Claude Code: every step explained](https://www.youtube.com/watch?v=hX7yG1KVYhI) · 44.3 分 — 講者以自己的「課程影片管理員」(Course Video Manager) 專案為例，實戰示範如何運用 Claude Code 完成一個小型
- [mattpocock/skills: A complete AI Coding workflow, end-to-end](https://www.youtube.com/watch?v=M6mYodf0dJM) · 17.3 分 — 本影片是 Matt Pocock 為其 GitHub skills 儲存庫所錄製的官方教學，帶領觀眾在 AI Hero CLI 這個全新的 
- [5 Claude Code skills I use every single day](https://www.youtube.com/watch?v=EJyuu6zlQCg) · 16.7 分 — 一位資深工程師分享他在使用 Claude Code 時所設計的一套「技能 (Skills)」，用來規範沒有記憶的 AI 代理工作流程
- [Ship working code while you sleep with the Ralph Wiggum technique](https://www.youtube.com/watch?v=_IK18goX4X8) · 16.4 分 — 影片介紹由 Jeffrey Huntley 提出的「Ralph Wigum」AI 編碼代理編排技術
- [I stopped using /grill-me for coding. Here’s what I use instead:](https://www.youtube.com/watch?v=6BB6exR8Zd8) · 15.3 分 — 作者分享他原本最受歡迎的「Grill Me」技能（用四句話寫成、讓 LLM 持續提問直到達成共識），以及他為何認為它仍有不足
- [New Skills! v1.1 brings /wayfinder, /research, /implement, /to-spec, /to-tickets](https://www.youtube.com/watch?v=A8mokin_YOs) · 15.2 分 — 本影片介紹 skills repo 的 v1.1 版本更新
- [/wayfinder: Nothing is too big to plan anymore](https://www.youtube.com/watch?v=F3lL98Pj90o) · 15.2 分 — 影片介紹作者開發的「Wayfinder」規劃技巧，用以解決 AI 代理單一會談無法處理大型工作的問題
- [9 Things People Get Wrong With My /grill-* skills](https://www.youtube.com/watch?v=UzMNBN6xLLA) · 13.5 分 — 本影片探討如何使用「Grill Me」與「Grill with Docs」這兩個用於 AI agent 規劃的技能，並列出常見的失敗模式與正
- [Can Cursor's HARDCORE Review Skill Stop The Slop?](https://www.youtube.com/watch?v=mh5XZ-L5SFQ) · 13.4 分 — 講者介紹並實測了 Cursor 團隊發布的「thermonuclear code quality review」skill
- [Learn anything with the /teach skill](https://www.youtube.com/watch?v=s5T5oQJcJ6U) · 13.1 分 — 講者 Matt 分享他自製的「Teach」技能：一個有狀態（stateful）的 AI 教學技能，會在使用者的檔案系統中建立任務、資源、HT
- [New Skills! /handoff, /prototype, /review and /writing-* | Skills Changelog](https://www.youtube.com/watch?v=DNqsMXH6Eog) · 12.8 分 — 影片介紹一個已在 GitHub 上突破 70k stars 的「skills repo」本週變更，主要內容包含兩個新技能「Handoff」與
- [Build An MCP Server In 5 Prompts // Vibe Coding That Doesn't Suck](https://www.youtube.com/watch?v=FRogt98OF80) · 12.7 分 — 本影片示範如何在五個提示詞（prompt）的輔助下，從空目錄開始建構一個 TypeScript MCP（Model Context Prot
- [/handoff is my new favourite skill](https://www.youtube.com/watch?v=dtAJ2dOd3ko) · 12.4 分 — 影片介紹作者開發的「handoff」技能，可將當前 AI 程式設計 session 的脈絡壓縮為 markdown 檔案，以便移交給另一個 
- [I was an AI skeptic. Then I tried plan mode](https://www.youtube.com/watch?v=WNx-s-RxVxk) · 12.3 分 — 影片介紹 Claude Code 的 Plan Mode（規劃模式）功能，說明為什麼在 AI 輔助編碼時應先規劃再執行，並分享如何透過 CL
- [I Open-Sourced My Own AFK Software Factory](https://www.youtube.com/watch?v=E5-QK3CDVQM) · 11.4 分 — 本影片介紹作者自製的 TypeScript 函式庫 Sandcastle，用於在隔離沙箱中編排 AI 編碼代理人（如 Claude Code
- [Claude Code tried to improve /init... Is it any better?](https://www.youtube.com/watch?v=llwTBpPqo9A) · 11.3 分 — 影片作者實測 Claude Code 團隊根據社群回饋（包含他自己的建議）所推出的新版 init 指令，並在個人 React Router 
- [How To De-Slop A Codebase Ruined By AI (with one skill)](https://www.youtube.com/watch?v=3MP8D-mdheA) · 11.3 分 — 本影片指出 AI 雖然加快了開發速度，卻也加速了「軟體熵」，使程式庫更快變成難以維護的「爛泥球 (ball of mud)」
- [Don't waste time on specs: /prototype instead](https://www.youtube.com/watch?v=n0VhIVtviC0) · 11.0 分 — 影片主張在與 AI 協作寫程式時，不要把全部精力投入撰寫極度詳細的規格（spec），而應該用「原型 (prototype)」這種可丟棄的程式
- [Anthropic's "dedicated monthly credit" is actually a huge cut](https://www.youtube.com/watch?v=lNOQaakmyDU) · 10.6 分 — Anthropic 公告自 6 月 15 日起，付費 Claude 訂閱方案可領取每月專屬「程式化用量額度」，用於 Claude Agent
- [Never Run claude /init](https://www.youtube.com/watch?v=9tmsq-Gvx6g) · 10.6 分 — 本影片主張不應執行 Claude Code 等編碼代理的 init 指令，因為自動產生的 CLAUDE.md / AGENTS.md 會浪費
- [Burn through the backlog from hell with /triage](https://www.youtube.com/watch?v=MzWIIlx0Gpc) · 10.3 分 — 影片介紹作者自製的「triage」技能，用於處理團隊協作中的待辦分診問題
- [How I use Claude Code for real engineering](https://www.youtube.com/watch?v=kZ-zzHVUrO4) · 10.2 分 — 本影片示範如何利用 Claude Code 處理一個超出單一 context window 的大型多階段功能開發任務
- [Most devs don’t understand how context windows work](https://www.youtube.com/watch?v=-uW5-TaVXu4) · 9.6 分 — 本影片針對開發者使用 AI 編碼代理（coding agents）時常見的「技能問題」，深入說明 LLM 上下文視窗（context win
- [AI SDK 6 is SWEET](https://www.youtube.com/watch?v=YCrj0E_P6ls) · 9.1 分 — 影片介紹 Vercel 在舊金山舞台上公布的 AI SDK 第 6 版 beta，強調 NPM 下載量在一年內從約 40 萬成長到近 400
- [Your codebase is NOT ready for AI (here's how to fix it)](https://www.youtube.com/watch?v=uC44zFz7JSM) · 8.8 分 — 影片主張 AI 為程式碼庫帶來嚴苛限制，現有程式碼庫多半尚未準備好；最大的影響因素並非提示詞或 agents.md，而是程式碼本身的設計
- [The 7 phases of AI-driven development](https://www.youtube.com/watch?v=Ah9p7v7nJWg) · 8.4 分 — 講者分享自己在使用 AI 程式設計助理（主要為 Claude Code）時所歸納出的七個開發階段：想法、研究、原型、PRD、看板、執行與 Q
- [I'm using claude --worktree for everything now](https://www.youtube.com/watch?v=yv8VZpov8bk) · 8.0 分 — 講者介紹並實測 Claude Code 新推出的內建 Git worktree 功能，示範手動建立 worktree 的流程，並比較 Cla
- [Most devs don't understand what agents are](https://www.youtube.com/watch?v=AtYtuVTZCQU) · 7.6 分 — 影片討論 OpenAI 推出的 Agent Kit，並引用 Anthropic 的「Building Effective Agents」文章
- [How to actually force Claude Code to use the right CLI (don't use CLAUDE.md)](https://www.youtube.com/watch?v=3CSi8QAoN-s) · 6.8 分 — 本影片介紹如何透過 Claude Code 的 pre-tool use hook，以確定性方式強制執行工作流程規則（例如禁止使用 npm、
- [Frontend is HARDER for AI than backend (here's how to fix it)](https://www.youtube.com/watch?v=pSritFeoYFo) · 5.5 分 — 本影片探討 AI 編碼代理在前端開發上的弱點，指出因為缺乏視覺回饋迴圈，使 LLM 難以處理複雜的 UI 互動
- [Red Green Refactor is OP With Claude Code](https://www.youtube.com/watch?v=hYZdIwFIy-c) · 5.3 分 — 影片介紹如何將經典的紅綠重構（Red-Green-Refactor，又稱測試驅動開發 TDD）流程應用於 AI 編碼代理人
- [Get Started With The Model Context Protocol // 2-Minute Tutorial](https://www.youtube.com/watch?v=MC2BwMGFRx4) · 2.4 分 — 本影片示範如何以單一 TypeScript 檔案（無需建置步驟）建立 MCP server，並透過 NPX 與 TSX 將其註冊到 Clau

### LLM 與 AI 開發概念（7 支）

- [A Complete Guide To Vercel’s AI SDK // The ESSENTIAL Tool For Shipping AI Apps](https://www.youtube.com/watch?v=mojZpktAiYQ) · 38.4 分 — 本影片是 Vercel AI SDK 的完整教學，從基礎的文字生成（generateText）、串流（streamText）、系統提示詞、多
- [I stopped using /grill-me for coding. Here’s what I use instead:](https://www.youtube.com/watch?v=6BB6exR8Zd8) · 15.3 分 — 作者分享他原本最受歡迎的「Grill Me」技能（用四句話寫成、讓 LLM 持續提問直到達成共識），以及他為何認為它仍有不足
- [Never Trust An LLM](https://www.youtube.com/watch?v=9VNG0h4pLh0) · 14.0 分 — 本影片由一位軟體開發者講解 LLM（大語言模型）的幻覺（hallucination）問題
- [Most devs don't understand how LLM tokens work](https://www.youtube.com/watch?v=nKSk_TiR8YA) · 10.9 分 — 本影片深入講解大型語言模型（LLM）的 token 機制
- [Most devs don’t understand how context windows work](https://www.youtube.com/watch?v=-uW5-TaVXu4) · 9.6 分 — 本影片針對開發者使用 AI 編碼代理（coding agents）時常見的「技能問題」，深入說明 LLM 上下文視窗（context win
- [AI SDK 6 is SWEET](https://www.youtube.com/watch?v=YCrj0E_P6ls) · 9.1 分 — 影片介紹 Vercel 在舊金山舞台上公布的 AI SDK 第 6 版 beta，強調 NPM 下載量在一年內從約 40 萬成長到近 400
- [Most devs don't understand what agents are](https://www.youtube.com/watch?v=AtYtuVTZCQU) · 7.6 分 — 影片討論 OpenAI 推出的 Agent Kit，並引用 Anthropic 的「Building Effective Agents」文章

### 狀態管理與狀態機（11 支）

- [XState - Global state in React](https://www.youtube.com/watch?v=1kJcnFBrk2I) · 84.9 分 — 這是一場在 Twitch 上的 XState Q&A 直播，主講人從零開始設計並實作一個認證（auth）狀態機，涵蓋登入／登出與 JWT 過
- [XState Docs Speedrun - 2022](https://www.youtube.com/watch?v=2eurRx-tR-I) · 68.8 分 — 本影片為一場 XState 文件的「速度跑」（speed run），作者透過 Stately 視覺化編輯器與 VS Code 擴充套件，依序
- [Checking out a new DSL for XState - Lucy!](https://www.youtube.com/watch?v=RklT33R6v10) · 68.1 分 — 主持人於 Twitch 直播中初次接觸並實測 Lucy，一個由 Matthew Phillips 為 XState 打造的全新 DSL（領域
- [Modelling Statecharts: A step-by-step guide](https://www.youtube.com/watch?v=wykDyFwr8Lk) · 19.4 分 — 影片以 Twitter 發文功能為例，示範從零開始建立 state chart 的步驟：先列出所有可能事件，再定義任務（actions 與 
- [Designing State Machines #3 - Video Call](https://www.youtube.com/watch?v=LrF4NHMB2L8) · 17.9 分 — 本影片以設計一個類似 Zoom 的視訊通話應用為例，逐步示範如何使用狀態機（statecharts）來管理從取得裝置權限、進入等候室、加入通
- [Designing State Machines #2 - Authentication](https://www.youtube.com/watch?v=BL3QQeOvqYo) · 14.2 分 — 本影片為「設計狀態機」系列第二集，示範如何為網頁應用程式設計一個非同步的身份驗證（authentication）狀態機流程
- [Designing State Machines #1 - Fetch Logic](https://www.youtube.com/watch?v=Km8QGtRr1z8) · 10.9 分 — Matt Pocock 透過 Excalidraw 從零設計一個 fetch 網路請求的狀態機，逐步列出狀態（pending、has dat
- [Burn through the backlog from hell with /triage](https://www.youtube.com/watch?v=MzWIIlx0Gpc) · 10.3 分 — 影片介紹作者自製的「triage」技能，用於處理團隊協作中的待辦分診問題
- [Should you still be using Redux in 2023?](https://www.youtube.com/watch?v=czptRA4IeCc) · 7.6 分 — 影片介紹 Redux 這個 React 生態圈中知名的狀態管理函式庫，說明它如何用「單一 store + actions + 訂閱」的模式取
- [Integrating Form Libraries (in React/Vue/Svelte) with XState](https://www.youtube.com/watch?v=Y5ZoVXjLqLA) · 7.1 分 — Matt 介紹如何將 XState 與表單函式庫（以 React Hook Form 為例）整合
- [XState Brain Teaser 2 - Async Validation](https://www.youtube.com/watch?v=nUKA0xhkZc8) · 2.8 分 — 本影片為 XState 腦筋急轉彎，介紹一個具有四個輸入欄位的付款表單

### React 與前端開發（17 支）

- [XState - Global state in React](https://www.youtube.com/watch?v=1kJcnFBrk2I) · 84.9 分 — 這是一場在 Twitch 上的 XState Q&A 直播，主講人從零開始設計並實作一個認證（auth）狀態機，涵蓋登入／登出與 JWT 過
- [XState Docs Speedrun - 2022](https://www.youtube.com/watch?v=2eurRx-tR-I) · 68.8 分 — 本影片為一場 XState 文件的「速度跑」（speed run），作者透過 Stately 視覺化編輯器與 VS Code 擴充套件，依序
- [Should you still be using Redux in 2023?](https://www.youtube.com/watch?v=czptRA4IeCc) · 7.6 分 — 影片介紹 Redux 這個 React 生態圈中知名的狀態管理函式庫，說明它如何用「單一 store + actions + 訂閱」的模式取
- [Integrating Form Libraries (in React/Vue/Svelte) with XState](https://www.youtube.com/watch?v=Y5ZoVXjLqLA) · 7.1 分 — Matt 介紹如何將 XState 與表單函式庫（以 React Hook Form 為例）整合
- [Blazing Fast Tips: React & TypeScript](https://www.youtube.com/watch?v=37PafxU_uzQ) · 6.2 分 — 本影片介紹如何在 React（搭配 Next.js 專案）中有效使用 TypeScript
- [Learn tRPC in 5 minutes](https://www.youtube.com/watch?v=S6rcrkbsDI0) · 6.0 分 — 本影片為 tRPC 的快速入門教學，介紹 tRPC 作為一個使用 TypeScript 串接前後端、提供型別安全通訊的全端框架
- [Most React devs don’t understand generic components](https://www.youtube.com/watch?v=zqz-lEHjJr4) · 5.7 分 — 本影片介紹如何在 React 搭配 TypeScript 的情境下建立泛用（generic）元件，以 Table 元件為例，說明如何透過型別
- [Frontend is HARDER for AI than backend (here's how to fix it)](https://www.youtube.com/watch?v=pSritFeoYFo) · 5.5 分 — 本影片探討 AI 編碼代理在前端開發上的弱點，指出因為缺乏視覺回饋迴圈，使 LLM 難以處理複雜的 UI 互動
- [Vercel wants you to fetch data like this](https://www.youtube.com/watch?v=0tR89PcQRAc) · 4.1 分 — 影片介紹 Vercel 釋出的 React 資料抓取 Hook「useSWR」v2 主要更新內容，重點涵蓋新增的 useSWRMutatio
- [Dan Abramov asked me to help fix React](https://www.youtube.com/watch?v=M3TPmOpNFCs) · 3.9 分 — 影片作者分享自己在 Twitter 上向 Dan Abramov 指出 TypeScript 與 React 的型別問題，獲得 Dan 邀請
- [Rethink your life choices before you do this...](https://www.youtube.com/watch?v=uiOJ5_7cB4I) · 3.7 分 — 本影片說明如何使用 TypeScript 的區辨聯合（discriminated union）搭配 `as` prop，建構一個同時支援 b
- [Stop using defaultProps (React 18.3 preview)](https://www.youtube.com/watch?v=fnQusZt49_Y) · 3.5 分 — 影片介紹 React 生態圈的一則重要消息：在 React 18.3.0 中，函式型元件的 defaultProps 將在主控台顯示棄用警告
- [TypeScript just unblocked React Server Components](https://www.youtube.com/watch?v=DTYNJjKlzFs) · 2.8 分 — 影片介紹 TypeScript 5.1 beta 版的兩大重點更新：第一是放寬對 undefined 返回函式的型別推斷與檢查；第二是解耦 
- [Fix your useRefs with ElementRef](https://www.youtube.com/watch?v=JuULWGuoI1A) · 2.1 分 — 本影片介紹 React 中的 ElementRef 型別，說明如何利用它為 useRef 提供強型別，取代手動從 lib.dom.d.ts 
- [Extracting React Props using CONDITIONAL TYPES - Advanced TypeScript](https://www.youtube.com/watch?v=VBsKNKEZNnY) · 1.8 分 — 本影片介紹 TypeScript 中一個常見的模式：建立型別輔助函式（type helper）來從非型別的值中萃取型別
- [Vercel's OG image service is incredible](https://www.youtube.com/watch?v=qMmc0IwXa7U) · 1.8 分 — 這是一段關於 Vercel OG 圖片生成功能的教學影片，介紹如何從零開始建立一個 OG 圖片路由，涵蓋基礎的 handler 函式與 JS
- [React Components with GENERICS? - Advanced TypeScript](https://www.youtube.com/watch?v=vCq9QsDinvo) · 1.4 分 — 本影片說明如何在 React 元件中使用 TypeScript 泛型（generics），讓元件能接收任意型別的 items 並將型別傳遞給

### 程式碼架構與品質（8 支）

- [Can Cursor's HARDCORE Review Skill Stop The Slop?](https://www.youtube.com/watch?v=mh5XZ-L5SFQ) · 13.4 分 — 講者介紹並實測了 Cursor 團隊發布的「thermonuclear code quality review」skill
- [How To De-Slop A Codebase Ruined By AI (with one skill)](https://www.youtube.com/watch?v=3MP8D-mdheA) · 11.3 分 — 本影片指出 AI 雖然加快了開發速度，卻也加速了「軟體熵」，使程式庫更快變成難以維護的「爛泥球 (ball of mud)」
- [Your codebase is NOT ready for AI (here's how to fix it)](https://www.youtube.com/watch?v=uC44zFz7JSM) · 8.8 分 — 影片主張 AI 為程式碼庫帶來嚴苛限制，現有程式碼庫多半尚未準備好；最大的影響因素並非提示詞或 agents.md，而是程式碼本身的設計
- [Red Green Refactor is OP With Claude Code](https://www.youtube.com/watch?v=hYZdIwFIy-c) · 5.3 分 — 影片介紹如何將經典的紅綠重構（Red-Green-Refactor，又稱測試驅動開發 TDD）流程應用於 AI 編碼代理人
- [Should you use return types?](https://www.youtube.com/watch?v=nwSe95uFN8E) · 4.7 分 — 本影片介紹 TypeScript 的函式回傳型別（return types），說明簡單函式可依賴型別推論、不必顯式標註；但含有多重分支（如 
- [This TS naming convention is weird](https://www.youtube.com/watch?v=qA65QjWCl60) · 4.0 分 — 本影片由 Matt Pocock 主講，介紹 TypeScript 中命名型別（type）的四個實用規則：型別名稱一律使用單數（陣列除外）、
- [My "as few deps as possible" monorepo setup](https://www.youtube.com/watch?v=hRyU0bN7qhw) · 4.0 分 — 介紹如何以最少依賴（pnpm、TypeScript、Turborepo）建立 monorepo，涵蓋目錄結構、turbo build 與 t
- [This saved me HOURS of refactoring time](https://www.youtube.com/watch?v=6l3Q9DjbQ7A) · 1.8 分 — 這段影片介紹一個 TypeScript 重構小技巧：當需要為部分使用者加上 `role` 屬性時，可以先在函數簽章中將屬性設為「可傳入 `u
