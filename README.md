# BEEP Workflow Review Exercise

- **Name:** Anshu Kumari
- **Registration No:** 12303714
- **Task:** AMULET Workflow Review Exercise

---

## Overview

This repository contains my feedback and findings after analyzing the BEEP workflow review agent prototype. I evaluated the codebase to find common execution bugs and noted key places where the application can be optimized.

---

## Identified Bugs & Issues

1. **State Loss During Async Transitions**
   - **Problem:** The agent loses context state while moving between intermediate steps in the workflow.
   - **Cause:** Async handlers aren't properly awaiting context updates before calling the next function.
   - **Effect:** Leads to intermittent failure when processing multi-step tasks.

2. **Unhandled Exceptions on Payload Input**
   - **Problem:** Sending missing fields or invalid JSON throws uncaught exceptions.
   - **Cause:** No strict validation models (like Pydantic) on incoming payloads.
   - **Effect:** App crashes on edge-case inputs instead of returning proper error responses.

3. **Truncated Error Logs**
   - **Problem:** Multiline stack traces are cut short during runtime error logs.
   - **Cause:** Basic string logger setup without structured logging support.

---

## Key Recommendations

- **Add Input Validation:** Wrap payload parsing with Pydantic models to catch invalid types early.
- **Implement Structured Logging:** Switch to standard JSON logger (`structlog`) to retain complete stack traces for debugging.
- **Retry Logic:** Add simple retry logic with exponential backoff for external API calls to avoid failures on temporary network spikes.

---
