"""Stateless offline adapter for `argos run --backend-command` demonstrations."""

import asyncio
import sys

from demo import SyntheticLLM

from argos.backends import LLMRequest

if __name__ == "__main__":
    request = LLMRequest.model_validate_json(sys.stdin.read())
    print(asyncio.run(SyntheticLLM().complete(request)))
