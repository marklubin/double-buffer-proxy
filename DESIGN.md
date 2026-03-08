# Double-Buffered Context Window Management

## Origin
Blog post: https://marklubin.me/posts/hopping-context-windows/
Author: Mark Lubin

## Core Algorithm

### Three Phases

1. **Checkpoint Phase (T)** — Triggered at configurable capacity threshold (default 70%)
   - Summarize current context into a checkpoint
   - Initialize back buffer seeded with that checkpoint

2. **Concurrent Phase (T → T')** — Normal operation continues
   - Agent keeps working in the active (front) buffer
   - Every new message is appended to BOTH front and back buffers
   - Back buffer = compressed old history + full-fidelity recent messages

3. **Swap Phase (T')** — Active buffer hits capacity wall
   - Swap: back buffer becomes the new active buffer
   - Old front buffer is discarded
   - Seamless — no pause, no stop-the-world

### Configurable Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `checkpoint_threshold` | 0.7 | Fraction of context capacity triggering checkpoint |
| `swap_threshold` | 0.95 | Fraction of context capacity triggering swap |
| `max_context_tokens` | model-dependent | Total context window size |
| `max_generations` | 5 | Max summary-on-summary layers before full renewal |
| `compression_debt_threshold` | None | Quality threshold for forcing renewal (alternative to max_generations) |
| `summary_instructions` | (sensible default) | Prompt template for summarization |

### Incremental Summary Accumulation ("Telomeres in Reverse")

Instead of doing a full renewal on every swap:
- Each generation appends compression debt to the running summary
- Summaries accumulate across multiple handoffs
- When cumulative degradation exceeds `compression_debt_threshold` or `max_generations`:
  - **Recurse**: summarize the summaries (meta-compression)
  - **Dump**: clean restart from scratch
- This amortizes the cost of full renewal across many handoffs

### Properties
- ~30% memory overhead during concurrent phase
- Zero extra inference cost (same summarization call, just earlier)
- Graceful degradation: burst traffic after checkpoint → stop-the-world (today's status quo)
- Summary quality is higher because model isn't at attention cliff when summarizing

## Target Frameworks

### 1. LangChain (langchain_v1 middleware)
- **Class**: `DoubleBufferMiddleware` extending `AgentMiddleware`
- **Hook**: `before_model()` / `abefore_model()`
- **Location**: `libs/langchain_v1/langchain/agents/middleware/double_buffer.py`
- **PR title**: `feat(langchain): add double-buffer context window middleware`

### 2. Semantic Kernel (Python SDK)
- **Class**: `ChatHistoryDoubleBufferReducer` extending `ChatHistoryReducer`
- **Location**: `python/semantic_kernel/contents/history_reducer/chat_history_double_buffer_reducer.py`
- **Decorator**: `@experimental`
- **PR title**: TBD (issue first per their process)

### 3. CrewAI
- TBD pending research

### 4. Letta
- TBD pending research — upgrade existing compaction engine

## Target OSS Coding Agents (V1 — implemented)

### 5. OpenCode (`anomalyco/opencode`) — Go/TypeScript
- **Status**: Implemented on `feat/double-buffer-context` branch
- **Bug fix**: Issue #13946 — skip `isOverflow()` on compaction summary messages (`processor.ts`)
- **Core module**: `packages/opencode/src/session/double-buffer.ts` — `DoubleBuffer` namespace
- **Integration**: `compaction.ts` (4 new exports), `prompt.ts` (swap + checkpoint hooks)
- **Config**: `checkpointThreshold` / `swapThreshold` added to compaction schema
- **Thresholds**: checkpoint 50%, swap 75% (relative to usable context)
- **Tests**: `packages/opencode/src/session/__tests__/double-buffer.test.ts`
- **Issues**: #13946, #8140, #11314, #2945, #4317, #12479

### 6. Cline (`cline/cline`) — TypeScript (VS Code extension)
- **Status**: Implemented on `feat/double-buffer-context` branch
- **Core module**: `src/core/context/context-management/DoubleBufferManager.ts`
- **Integration**: `ContextManager.ts` (doubleBuffer property + delegation methods)
- **Thresholds**: checkpoint 60%, swap 85% (relative to contextWindow)
- **Tests**: `src/core/context/context-management/__tests__/DoubleBufferManager.test.ts`
- **Issues**: #4389, #827, #9181, #3331, #7379

### 7. Aider (`paul-gauthier/aider`) — Python
- **Status**: Implemented on `feat/double-buffer-context` branch
- **Core module**: `aider/double_buffer.py` — `DoubleBufferManager` class
- **Integration**: `aider/coders/base_coder.py` (`summarize_start` + `_double_buffer_check`)
- **CLI flags**: `--double-buffer`, `--checkpoint-threshold`, `--swap-threshold`
- **Thresholds**: checkpoint 60%, swap 85% (relative to max_chat_history_tokens)
- **Tests**: `tests/basic/test_double_buffer.py` (18 tests, all passing)
- **Issues**: #3607, #3445, #4113, #4583, #4418

## V1 Simplifications (all coding agent targets)

- **2-state**: `has_checkpoint` / `no_checkpoint`. No formal CONCURRENT buffer duplication.
- **No tool masking** — separate concern (proxy handles this).
- **No generation limits** — single cycle. Multi-generation is follow-up.
- **Sync fallback** — if checkpoint not ready at swap, block and summarize (aider) or fall through to standard compaction (OpenCode, Cline).
