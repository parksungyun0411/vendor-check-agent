PY ?= uv run python
COMPANIES ?= data/companies.csv
TRUTH ?= data/ground_truth.jsonl
LIMIT ?= 0

.PHONY: install mcp proxy groundtruth baseline agent inject inject-confirm eval report check test lint

install:
	uv sync --extra dev --extra llm

mcp:  ## 기관별 툴 MCP 서버 (stdio)
	$(PY) -m vendor_check.mcp_server

proxy:  ## 실패 주입 프록시 단독 실행 (FAULT_PLAN=faults/example_plan.json)
	$(PY) -m vendor_check.proxy

groundtruth:  ## 정답 생성: 기관 API 직접 호출 (키 필요)
	$(PY) -m vendor_check.eval.groundtruth --companies $(COMPANIES) --out $(TRUTH)

baseline:  ## 대조군, 실패 주입 없음
	$(PY) -m vendor_check.eval.run --truth $(TRUTH) --systems baseline --strategies none \
		--scenarios clean --limit $(LIMIT) --out reports/runs/baseline.jsonl

agent:  ## LangGraph 에이전트, 실패 주입 없음
	$(PY) -m vendor_check.eval.run --truth $(TRUTH) --systems agent --strategies none \
		--scenarios clean --limit $(LIMIT) --out reports/runs/agent.jsonl

inject:  ## 실패 시나리오 전체 × 복구 전략 4종 × 두 시스템
	$(PY) -m vendor_check.eval.run --truth $(TRUTH) --limit $(LIMIT) --out reports/runs/inject.jsonl

inject-confirm:  ## 같은 행렬을 동명 법인 확인을 준 대조군으로 (baseline+confirm 으로 집계)
	$(PY) -m vendor_check.eval.run --truth $(TRUTH) --systems baseline --baseline-approver truth \
		--limit $(LIMIT) --out reports/runs/inject-confirm.jsonl

eval:  ## reports/runs/*.jsonl → reports/metrics.json → reports/RESULTS.md
	$(PY) -m vendor_check.eval.report metrics --truth $(TRUTH)
	$(PY) -m vendor_check.eval.report render

report:  ## metrics.json 으로 결과표만 다시 그린다
	$(PY) -m vendor_check.eval.report render

check:  ## 한 곳 대화형 점검: make check NAME=상호명
	$(PY) -m vendor_check.cli check "$(NAME)"

test:
	uv run pytest -q

lint:
	uv run ruff check .
