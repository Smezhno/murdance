"""Prompt regression test runner.

Per CONTRACT §21:
- YAML test suites: user input → expected (contains/not_contains/tool_calls)
- Stability: temperature=0, 3 runs, pass if 2/3
- Suite threshold: ≥ 90%
- Run: python -m tests.prompt_regression.runner
"""

import asyncio
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.core.booking_flow import get_booking_flow
from app.models import UnifiedMessage
from datetime import datetime, timezone


class TestRunner:
    """Prompt regression test runner (CONTRACT §21)."""

    def __init__(self) -> None:
        """Initialize test runner."""
        self.booking_flow = get_booking_flow()
        self.runs_per_test = 3  # Stability: 3 runs, pass if 2/3
        self.suite_threshold = 0.90  # ≥ 90% must pass

    async def run_test_case(
        self,
        test_case: dict[str, Any],
        conversation_history: list[dict[str, str]],
    ) -> tuple[bool, str, str]:
        """Run a single test case.

        Args:
            test_case: Test case dict with 'user' and 'expected'
            conversation_history: Previous messages in conversation

        Returns:
            Tuple of (passed, response_text, error_message)
        """
        user_input = test_case.get("user", "")
        expected = test_case.get("expected", {})

        # Create UnifiedMessage
        message = UnifiedMessage(
            channel="telegram",
            chat_id="test_chat",
            message_id=str(uuid4()),
            timestamp=datetime.now(timezone.utc),
            text=user_input,
            message_type="text",
            sender_phone=None,
            sender_name="Test User",
        )

        # Process message
        trace_id = uuid4()
        response_text = await self.booking_flow.process_message(message, trace_id)

        # Check expectations
        passed = True
        errors = []

        # Check contains
        contains = expected.get("contains", [])
        for phrase in contains:
            if phrase.lower() not in response_text.lower():
                passed = False
                errors.append(f"Missing phrase: '{phrase}'")

        # Check not_contains
        not_contains = expected.get("not_contains", [])
        for phrase in not_contains:
            if phrase.lower() in response_text.lower():
                passed = False
                errors.append(f"Unexpected phrase: '{phrase}'")

        # TODO: tool_calls validation is not implemented yet
        # Would require access to tool execution results from booking_flow
        # For now, tool_calls in YAML are for documentation only

        error_msg = "; ".join(errors) if errors else ""
        return passed, response_text, error_msg

    async def run_test_suite(self, test_file: Path) -> dict[str, Any]:
        """Run a test suite from YAML file.

        Args:
            test_file: Path to YAML test file

        Returns:
            Dict with test results
        """
        with open(test_file, "r", encoding="utf-8") as f:
            suite_data = yaml.safe_load(f)

        suite_name = suite_data.get("name", test_file.stem)
        tests = suite_data.get("tests", [])

        results = {
            "suite_name": suite_name,
            "total_tests": len(tests),
            "passed": 0,
            "failed": 0,
            "test_results": [],
        }

        conversation_history: list[dict[str, str]] = []

        for test in tests:
            test_name = test.get("name", "unnamed")
            print(f"  Running: {test_name}...", end=" ")

            # Run test 3 times for stability (CONTRACT §21)
            passes = 0
            for run in range(self.runs_per_test):
                passed, response, error_msg = await self.run_test_case(test, conversation_history)
                if passed:
                    passes += 1

            # Pass if 2/3 runs pass (CONTRACT §21)
            test_passed = passes >= 2

            if test_passed:
                results["passed"] += 1
                print("✓ PASS")
            else:
                results["failed"] += 1
                print(f"✗ FAIL ({passes}/{self.runs_per_test} runs passed)")

            results["test_results"].append({
                "name": test_name,
                "passed": test_passed,
                "passes": passes,
                "runs": self.runs_per_test,
            })

            # Update conversation history
            user_text = test.get("user", "")
            if user_text:
                conversation_history.append({"role": "user", "content": user_text})
                # Keep last 10 messages
                conversation_history = conversation_history[-10:]

        return results

    async def run_all_suites(self) -> int:
        """Run all test suites.

        Returns:
            Exit code (0 = success, 1 = failure)
        """
        test_dir = Path(__file__).parent
        test_files = sorted(test_dir.glob("test_*.yaml"))

        if not test_files:
            print("No test files found!")
            return 1

        print(f"Running {len(test_files)} test suite(s)...\n")

        all_results = []
        total_tests = 0
        total_passed = 0

        for test_file in test_files:
            print(f"Suite: {test_file.stem}")
            results = await self.run_test_suite(test_file)
            all_results.append(results)

            total_tests += results["total_tests"]
            total_passed += results["passed"]

            print(f"  Passed: {results['passed']}/{results['total_tests']}\n")

            # Cleanup between test suites: reset session for test_chat
            from app.storage.redis import redis_storage
            await redis_storage.delete("session:telegram:test_chat")

        # Calculate suite pass rate
        pass_rate = total_passed / total_tests if total_tests > 0 else 0.0

        print("=" * 60)
        print(f"Total: {total_passed}/{total_tests} tests passed ({pass_rate:.1%})")
        print(f"Threshold: {self.suite_threshold:.0%} (CONTRACT §21)")

        # Check if suite threshold met (≥ 90%)
        if pass_rate >= self.suite_threshold:
            print("✓ Suite threshold met")
            return 0
        else:
            print(f"✗ Suite threshold NOT met ({pass_rate:.1%} < {self.suite_threshold:.0%})")
            return 1


async def main() -> int:
    """Main entry point."""
    # Setup: initialize Redis connection and load KB
    from app.storage.redis import redis_storage
    from app.knowledge.base import load_knowledge_base

    try:
        await redis_storage.connect()
        load_knowledge_base()  # Raises if invalid - test must not start

        runner = TestRunner()
        exit_code = await runner.run_all_suites()

        # Teardown: disconnect Redis
        await redis_storage.disconnect()

        return exit_code
    except Exception as e:
        print(f"Setup failed: {e}")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

