# 환경 버전 기록 (프로젝트 코드 버전과 별도 관리)

환경 버전은 코드 버전(`pyproject.toml`의 `version`, git SHA·태그)과 **따로** 관리합니다. 환경이 바뀌면(OS 빌드, Python, uv, 핵심 라이브러리, 런너) 새 ENV 항목을 추가하고, 그 이후의 실험과 trial 기록에는 환경 ID를 남깁니다(Phase 2 `research.db`의 `env_id`).

## ENV-2026.11.09 (현재)
| 항목 | 값 | 확인 방법 |
|---|---|---|
| OS | Windows 11 Home 10.0.26200.9711 | `ver` |
| Python | 3.13.5 (MSC v.1943 64 bit) | `uv run python -c "import sys; print(sys.version)"` |
| uv | 0.13.7 (2026-11-03) | `uv --version` |
| polars | 2.1.0 | `uv.lock` |
| pydantic | 2.14.3 | `uv.lock` |
| duckdb | 2.0.0 | `uv.lock` |
| numba | ≥ 0.68.1 (`uv.lock` 고정) | `uv.lock` |
| hypothesis | 6.170.15 | `uv.lock` |
| optuna | 5.1.0 (고정, 2026-11-19 사용자 승인; alembic·sqlalchemy·colorlog·pyyaml·tqdm 동반) | `pyproject.toml`, `uv.lock` |
| cryptography | 50.0.2 (고정, 2026-11-19 승인된 계획의 Phase 7 의존성; pycparser 동반) | `pyproject.toml`, `uv.lock` |
| CI 런너 | GitHub Actions `ubuntu-latest`(SHA 고정 actions) | `.github/workflows/ci.yml` |
| 하드웨어 | RAM 16GB, SSD KLEVV C710(과열 이력) | - |

## 코드 버전
| 코드 버전 | git | 의미 |
|---|---|---|
| 0.1.0 | `e1bac5c` 이전 | Phase 0~1 구현 |
| 0.2.0 | Phase 2 착수 커밋부터 | Phase 1 상태 IMPLEMENTED / OPERATIONAL_VALIDATION_PENDING. Phase 2(DSL·백테스트) 개발 |
