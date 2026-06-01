# model_pricing_seed.md

- 查價日期：2026-06-01（Asia/Taipei）
- 用途：Reward Hacking V1.5 Agent FinOps Core 的官方模型價格預設資料。
- 建議流程：Markdown 供人審查，YAML 供系統匯入；Admin dry-run 後才 apply。
- 原則：價格異動時不得覆蓋舊 row，應使用 `effective_to` + `supersedes_pricing_id` 建立新版本。
- 注意：MiniMax M3 目前含短期促銷價，Kimi/MiniMax 等動態頁價格建議 Admin 匯入前再次人工確認。

## Source Documents

| Provider | Source URL | Confidence | Checked At |
|---|---|---|---|
| openai | https://openai.com/api/pricing/ | official | 2026-06-01 |
| google | https://ai.google.dev/gemini-api/docs/pricing | official | 2026-06-01 |
| google | https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing | official | 2026-06-01 |
| anthropic | https://docs.anthropic.com/en/docs/about-claude/pricing | official | 2026-06-01 |
| deepseek | https://api-docs.deepseek.com/quick_start/pricing | official | 2026-06-01 |
| kimi | https://platform.moonshot.ai/ | official | 2026-06-01 |
| kimi | https://platform.moonshot.ai/docs/pricing/tools | official | 2026-06-01 |
| minimax | https://platform.minimax.io/docs/guides/pricing-paygo | official | 2026-06-01 |
| zai | https://docs.z.ai/guides/overview/pricing | official | 2026-06-01 |

## Pricing Items Summary

### OpenAI

| Model / Resource | Tier | Mode | Token Type | Unit Price | Unit | Currency | Conditions / Notes | Source |
|---|---|---|---|---:|---|---|---|---|
| gpt-5.5 | premium_reasoning | standard | input | 5.00 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.5 | premium_reasoning | standard | cached_input | 0.50 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.5 | premium_reasoning | standard | output | 30.00 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.5 | premium_reasoning | batch | input | 2.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>OpenAI Batch API: 50% discount on inputs and outputs; confirm model eligibility before use. | https://openai.com/api/pricing/ |
| gpt-5.5 | premium_reasoning | batch | output | 15.0 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>OpenAI Batch API: 50% discount on inputs and outputs; confirm model eligibility before use. | https://openai.com/api/pricing/ |
| gpt-5.4 | premium | standard | input | 2.50 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.4 | premium | standard | cached_input | 0.25 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.4 | premium | standard | output | 15.00 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.4 | premium | batch | input | 1.25 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>OpenAI Batch API: 50% discount on inputs and outputs; confirm model eligibility before use. | https://openai.com/api/pricing/ |
| gpt-5.4 | premium | batch | output | 7.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>OpenAI Batch API: 50% discount on inputs and outputs; confirm model eligibility before use. | https://openai.com/api/pricing/ |
| gpt-5.4-mini | standard | standard | input | 0.75 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.4-mini | standard | standard | cached_input | 0.075 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.4-mini | standard | standard | output | 4.50 | per_1m_tokens | USD |  | https://openai.com/api/pricing/ |
| gpt-5.4-mini | standard | batch | input | 0.375 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>OpenAI Batch API: 50% discount on inputs and outputs; confirm model eligibility before use. | https://openai.com/api/pricing/ |
| gpt-5.4-mini | standard | batch | output | 2.25 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>OpenAI Batch API: 50% discount on inputs and outputs; confirm model eligibility before use. | https://openai.com/api/pricing/ |
| web_search | tool | standard | not_applicable | 10.00 | per_1k_calls | USD | OpenAI Web Search tool fixed fee; search content token handling follows official API pricing page. | https://openai.com/api/pricing/ |
| containers-1gb | runtime | standard | not_applicable | 0.03 | per_container_session | USD | OpenAI Containers 1GB listed unit; verify billing mode and session policy before production import. | https://openai.com/api/pricing/ |
| containers-64gb | runtime | standard | not_applicable | 1.92 | per_container_session | USD | OpenAI Containers 64GB listed unit; verify billing mode and session policy before production import. | https://openai.com/api/pricing/ |

### Google Gemini

| Model / Resource | Tier | Mode | Token Type | Unit Price | Unit | Currency | Conditions / Notes | Source |
|---|---|---|---|---:|---|---|---|---|
| gemini-3.5-flash | standard | standard | input | 1.50 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.5-flash | standard | standard | output | 9.00 | per_1m_tokens | USD | Gemini output price includes thinking tokens where applicable per official pricing page. | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.5-flash | standard | standard | cached_input | 0.15 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.5-flash | standard | standard | cache_storage | 1.00 | per_1m_tokens_hour | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-pro-preview | premium | standard | input | 2.00 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-pro-preview | premium | standard | output | 12.00 | per_1m_tokens | USD | Gemini output price includes thinking tokens where applicable per official pricing page. | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-pro-preview | premium | standard | cached_input | 0.20 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-pro-preview | premium | standard | cache_storage | 4.50 | per_1m_tokens_hour | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-pro-preview | premium | standard | input | 4.00 | per_1m_tokens | USD | input_min=200001<br>Long-context tier for prompts above 200k tokens. | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-pro-preview | premium | standard | output | 18.00 | per_1m_tokens | USD | input_min=200001<br>Long-context tier for prompts above 200k tokens. | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-pro-preview | premium | standard | cached_input | 0.40 | per_1m_tokens | USD | input_min=200001<br>Long-context cached-input tier for prompts above 200k tokens. | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-flash-lite | cheap | standard | input | 0.25 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-flash-lite | cheap | standard | output | 1.50 | per_1m_tokens | USD | Gemini output price includes thinking tokens where applicable per official pricing page. | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-flash-lite | cheap | standard | cached_input | 0.025 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-flash-lite | cheap | standard | cache_storage | 1.00 | per_1m_tokens_hour | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3.1-flash-lite | cheap | standard | audio_input | 0.50 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3-flash-preview | standard | standard | input | 0.50 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3-flash-preview | standard | standard | output | 3.00 | per_1m_tokens | USD | Gemini output price includes thinking tokens where applicable per official pricing page. | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3-flash-preview | standard | standard | cached_input | 0.05 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3-flash-preview | standard | standard | cache_storage | 1.00 | per_1m_tokens_hour | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| gemini-3-flash-preview | standard | standard | audio_input | 1.00 | per_1m_tokens | USD |  | https://ai.google.dev/gemini-api/docs/pricing |
| grounding-with-google-search-gemini-3 | tool | standard | not_applicable | 14.00 | per_1k_calls | USD | free_quota=5000/monthly<br>Grounding with Google Search / Maps: 5,000 prompts per month free, then listed per-1,000 search queries for Gemini 3 family. | https://ai.google.dev/gemini-api/docs/pricing |

### Anthropic Claude

| Model / Resource | Tier | Mode | Token Type | Unit Price | Unit | Currency | Conditions / Notes | Source |
|---|---|---|---|---:|---|---|---|---|
| claude-opus-4.8 | premium_reasoning | standard | input | 5.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.8 | premium_reasoning | standard | cache_write_5m | 6.25 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.8 | premium_reasoning | standard | cache_write_1h | 10.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.8 | premium_reasoning | standard | cached_input | 0.50 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.8 | premium_reasoning | standard | output | 25.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.8 | premium_reasoning | batch | input | 2.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.8 | premium_reasoning | batch | output | 12.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.7 | premium_reasoning | standard | input | 5.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.7 | premium_reasoning | standard | cache_write_5m | 6.25 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.7 | premium_reasoning | standard | cache_write_1h | 10.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.7 | premium_reasoning | standard | cached_input | 0.50 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.7 | premium_reasoning | standard | output | 25.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.7 | premium_reasoning | batch | input | 2.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.7 | premium_reasoning | batch | output | 12.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.6 | premium_reasoning | standard | input | 5.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.6 | premium_reasoning | standard | cache_write_5m | 6.25 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.6 | premium_reasoning | standard | cache_write_1h | 10.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.6 | premium_reasoning | standard | cached_input | 0.50 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.6 | premium_reasoning | standard | output | 25.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.6 | premium_reasoning | batch | input | 2.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-opus-4.6 | premium_reasoning | batch | output | 12.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-sonnet-4.6 | standard_reasoning | standard | input | 3.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-sonnet-4.6 | standard_reasoning | standard | cache_write_5m | 3.75 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-sonnet-4.6 | standard_reasoning | standard | cache_write_1h | 6.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-sonnet-4.6 | standard_reasoning | standard | cached_input | 0.30 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-sonnet-4.6 | standard_reasoning | standard | output | 15.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-sonnet-4.6 | standard_reasoning | batch | input | 1.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-sonnet-4.6 | standard_reasoning | batch | output | 7.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-haiku-4.5 | cheap | standard | input | 1.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-haiku-4.5 | cheap | standard | cache_write_5m | 1.25 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-haiku-4.5 | cheap | standard | cache_write_1h | 2.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-haiku-4.5 | cheap | standard | cached_input | 0.10 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-haiku-4.5 | cheap | standard | output | 5.00 | per_1m_tokens | USD |  | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-haiku-4.5 | cheap | batch | input | 0.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| claude-haiku-4.5 | cheap | batch | output | 2.5 | per_1m_tokens | USD | service_tier=batch<br>discount=0.50<br>Anthropic Batch API listed at 50% off for input/output tokens. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| web_search | tool | standard | not_applicable | 10.00 | per_1k_calls | USD | Anthropic web search tool fixed fee; model token cost still applies to generated/processed content. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| code_execution_container | runtime | standard | not_applicable | 0.05 | per_hour | USD | free_quota=1550/monthly<br>Claude code execution tool: monthly free hours then per-hour container charge; minimum billing may apply. | https://docs.anthropic.com/en/docs/about-claude/pricing |
| managed_agents_runtime | runtime | standard | not_applicable | 0.08 | per_hour | USD | Managed Agents runtime session-hour charge; model tokens billed separately. | https://docs.anthropic.com/en/docs/about-claude/pricing |

### DeepSeek

| Model / Resource | Tier | Mode | Token Type | Unit Price | Unit | Currency | Conditions / Notes | Source |
|---|---|---|---|---:|---|---|---|---|
| deepseek-v4-flash | cheap_reasoning | standard | cached_input | 0.0028 | per_1m_tokens | USD | DeepSeek input cache hit price. | https://api-docs.deepseek.com/quick_start/pricing |
| deepseek-v4-flash | cheap_reasoning | standard | input | 0.14 | per_1m_tokens | USD | DeepSeek input cache miss price. | https://api-docs.deepseek.com/quick_start/pricing |
| deepseek-v4-flash | cheap_reasoning | standard | output | 0.28 | per_1m_tokens | USD |  | https://api-docs.deepseek.com/quick_start/pricing |
| deepseek-v4-pro | standard_reasoning | standard | cached_input | 0.003625 | per_1m_tokens | USD | DeepSeek input cache hit price. | https://api-docs.deepseek.com/quick_start/pricing |
| deepseek-v4-pro | standard_reasoning | standard | input | 0.435 | per_1m_tokens | USD | DeepSeek input cache miss price. | https://api-docs.deepseek.com/quick_start/pricing |
| deepseek-v4-pro | standard_reasoning | standard | output | 0.87 | per_1m_tokens | USD |  | https://api-docs.deepseek.com/quick_start/pricing |

### Kimi / Moonshot

| Model / Resource | Tier | Mode | Token Type | Unit Price | Unit | Currency | Conditions / Notes | Source |
|---|---|---|---|---:|---|---|---|---|
| kimi-k2.6 | premium_agent | standard | cached_input | 0.16 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| kimi-k2.6 | premium_agent | standard | input | 0.95 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| kimi-k2.6 | premium_agent | standard | output | 4.00 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| kimi-k2.5 | standard_agent | standard | cached_input | 0.10 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| kimi-k2.5 | standard_agent | standard | input | 0.60 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| kimi-k2.5 | standard_agent | standard | output | 3.00 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| moonshot-v1 | legacy | standard | input | 2.00 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| moonshot-v1 | legacy | standard | output | 5.00 | per_1m_tokens | USD |  | https://platform.moonshot.ai/ |
| builtin_function.$web_search | tool | standard | not_applicable | 0.005 | per_call | USD | Kimi built-in $web_search tool call fee; search content token billing depends on whether content enters context. | https://platform.moonshot.ai/docs/pricing/tools |

### MiniMax

| Model / Resource | Tier | Mode | Token Type | Unit Price | Unit | Currency | Conditions / Notes | Source |
|---|---|---|---|---:|---|---|---|---|
| minimax-m3 | standard_agent | promotion | input | 0.30 | per_1m_tokens | USD | input_max=512000 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | standard_agent | promotion | output | 1.20 | per_1m_tokens | USD | input_max=512000 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | standard_agent | promotion | cached_input | 0.06 | per_1m_tokens | USD | input_max=512000<br>7-day 50% off promotional price for <=512k input tokens; verify expiry before production import. Prompt caching read price. | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | standard_agent | standard | input | 1.20 | per_1m_tokens | USD | input_min=512001 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | standard_agent | standard | output | 4.80 | per_1m_tokens | USD | input_min=512001 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | standard_agent | standard | cached_input | 0.24 | per_1m_tokens | USD | input_min=512001<br>>512k input-token tier; limited quantity / staged availability per official pricing page. Prompt caching read price. | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | priority_agent | promotion | input | 0.45 | per_1m_tokens | USD | service_tier=priority<br>input_max=512000 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | priority_agent | promotion | output | 1.80 | per_1m_tokens | USD | service_tier=priority<br>input_max=512000 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | priority_agent | promotion | cached_input | 0.09 | per_1m_tokens | USD | service_tier=priority<br>input_max=512000<br>Priority tier, 7-day 50% off promotional price for <=512k input tokens; verify expiry before production import. Prompt caching read price. | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | priority_agent | priority | input | 1.80 | per_1m_tokens | USD | service_tier=priority<br>input_min=512001 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | priority_agent | priority | output | 7.20 | per_1m_tokens | USD | service_tier=priority<br>input_min=512001 | https://platform.minimax.io/docs/guides/pricing-paygo |
| minimax-m3 | priority_agent | priority | cached_input | 0.36 | per_1m_tokens | USD | service_tier=priority<br>input_min=512001<br>Priority >512k input-token tier; early access / staged availability per official pricing page. Prompt caching read price. | https://platform.minimax.io/docs/guides/pricing-paygo |

### Z.AI / GLM

| Model / Resource | Tier | Mode | Token Type | Unit Price | Unit | Currency | Conditions / Notes | Source |
|---|---|---|---|---:|---|---|---|---|
| glm-5.1 | premium_agent | standard | input | 1.40 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5.1 | premium_agent | standard | cached_input | 0.26 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5.1 | premium_agent | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-5.1 | premium_agent | standard | output | 4.40 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5 | premium_agent | standard | input | 1.00 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5 | premium_agent | standard | cached_input | 0.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5 | premium_agent | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-5 | premium_agent | standard | output | 3.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5-turbo | standard_agent | standard | input | 1.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5-turbo | standard_agent | standard | cached_input | 0.24 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-5-turbo | standard_agent | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-5-turbo | standard_agent | standard | output | 4.00 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.7 | standard | standard | input | 0.60 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.7 | standard | standard | cached_input | 0.11 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.7 | standard | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-4.7 | standard | standard | output | 2.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.7-flashx | cheap | standard | input | 0.07 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.7-flashx | cheap | standard | cached_input | 0.01 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.7-flashx | cheap | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-4.7-flashx | cheap | standard | output | 0.40 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.6 | standard | standard | input | 0.60 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.6 | standard | standard | cached_input | 0.11 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.6 | standard | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-4.6 | standard | standard | output | 2.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5 | standard | standard | input | 0.60 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5 | standard | standard | cached_input | 0.11 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5 | standard | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-4.5 | standard | standard | output | 2.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-x | premium | standard | input | 2.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-x | premium | standard | cached_input | 0.45 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-x | premium | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-x | premium | standard | output | 8.90 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-air | cheap | standard | input | 0.20 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-air | cheap | standard | cached_input | 0.03 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-air | cheap | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-air | cheap | standard | output | 1.10 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-airx | standard | standard | input | 1.10 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-airx | standard | standard | cached_input | 0.22 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-airx | standard | standard | cache_storage | 0.00 | per_1m_tokens_hour | USD | Official page lists cached input storage as limited-time free; treat as temporary and review regularly. | https://docs.z.ai/guides/overview/pricing |
| glm-4.5-airx | standard | standard | output | 4.50 | per_1m_tokens | USD |  | https://docs.z.ai/guides/overview/pricing |
| web_search | tool | standard | not_applicable | 0.01 | per_call | USD | Z.AI built-in Web Search pricing. | https://docs.z.ai/guides/overview/pricing |
| glm-image | tool | standard | image_output | 0.015 | per_image | USD | Z.AI image generation pricing; verify model availability before production use. | https://docs.z.ai/guides/overview/pricing |
| cogvideox-3 | tool | standard | video_output | 0.20 | per_video | USD | Z.AI video generation pricing; verify model availability before production use. | https://docs.z.ai/guides/overview/pricing |
| glm-asr-2512 | tool | standard | audio_input | 0.03 | per_1m_tokens | USD | Z.AI ASR token pricing; official page notes approximate per-minute equivalent. | https://docs.z.ai/guides/overview/pricing |

## Recommended Admin Import Policy

| Item | Recommendation |
|---|---|
| Import mode | Admin dry-run first, then apply |
| Update behavior | Never overwrite; always supersede old price rows |
| Historical cost | Cost events must store pricing_snapshot |
| Editing | Simple edit only exposes unit_price, currency, effective dates, active flag, change_reason |
| Advanced edit | Restricted to Super Admin / FinOps Admin |
| Automatic scraping | Not recommended for V1.5 |

## YAML Import Notes

- The YAML file stores `unit_price` as strings to avoid floating-point precision issues; parse with Decimal.
- `source_checked_at` and `effective_from` are set to the checked date for initial seed import.
- `scope_type=system_default`; tenant-specific contract prices should be inserted as `tenant_override` rows.
- Rows with promotional pricing should be reviewed before production import and may require `effective_to`.