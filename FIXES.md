# BEEP Workflow Review – Bug Fixes & Code Implementation

This document contains code-level fixes and solutions for the issues identified during the BEEP Workflow Review Exercise.

---

## Code Solution (`fixes.py`)

```python
import asyncio
import logging
import traceback
from typing import Optional, Dict, Any, Callable
from pydantic import BaseModel, Field, ValidationError

# 1. Payload validation schema
class WorkflowPayload(BaseModel):
    task_id: str
    action: str
    data: Dict[str, Any] = Field(default_factory=dict)
    retry_count: Optional[int] = 0

def parse_and_validate_payload(raw_data: dict) -> Optional[WorkflowPayload]:
    try:
        return WorkflowPayload(**raw_data)
    except ValidationError as e:
        logger.error(f"Payload validation error: {e}")
        return None

# 2. Async state manager to avoid race conditions
class WorkflowStateManager:
    def __init__(self):
        self._state: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def update_state(self, key: str, value: Any) -> None:
        async with self._lock:
            self._state[key] = value

    async def get_state(self, key: str) -> Any:
        async with self._lock:
            return self._state.get(key)

# 3. Logging setup to capture full tracebacks
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("beep_agent")

def log_workflow_error(error: Exception, context: str) -> None:
    logger.error(
        f"Error in {context}: {str(error)}\n"
        f"Traceback:\n{traceback.format_exc()}"
    )

# 4. Retry strategy with backoff
async def execute_with_retry(async_func: Callable, retries: int = 3, delay: float = 1.0) -> Any:
    for attempt in range(1, retries + 1):
        try:
            return await async_func()
        except Exception as e:
            log_workflow_error(e, context=f"Attempt {attempt}/{retries}")
            if attempt == retries:
                raise e
            await asyncio.sleep(delay * (2 ** (attempt - 1)))

# Example run
async def main():
    state_mgr = WorkflowStateManager()
    
    sample_raw_data = {
        "task_id": "TASK_101",
        "action": "REVIEW_WORKFLOW",
        "data": {"status": "pending"}
    }
    
    payload = parse_and_validate_payload(sample_raw_data)
    if payload:
        await state_mgr.update_state("current_task", payload.task_id)
        current_task = await state_mgr.get_state("current_task")
        logger.info(f"Loaded task: {current_task}")

if __name__ == "__main__":
    asyncio.run(main())
