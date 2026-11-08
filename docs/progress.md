# 진행 현황 (Phase 0~1)

> 기준: `docs/plans/2026-11-08-phase0-1-build.md`(실행 계획, 승인 2026-11-08) / 설계 v2.1
> 상태 값: TODO / IN_PROGRESS / DONE / PARTIAL / BLOCKED. DONE은 완료 조건, 선언 게이트, CI를 모두 통과했을 때만 씁니다.
> 재개 방법: 이 파일에서 첫 번째로 DONE이 아닌 작업을 찾고, 실행 계획의 같은 단계 번호를 함께 읽습니다.

| # | 작업 | 상태 | 마지막 갱신 |
|---|---|---|---|
| 1 | Scaffold와 저장소 | IN_PROGRESS | 2026-11-08 |
| 2 | Phase 0 측정 | TODO | |
| 3 | 원본 캡처기 최소판 | TODO | |
| 4 | 조기 백업과 복원 리허설 | TODO | |
| 5 | 카탈로그 스키마 | TODO | |
| 6 | as_of / join_asof API | TODO | |
| 7 | 컴팩션과 정규화 | TODO | |
| 8 | Binance 벌크 T1 | TODO | |
| 9 | Upbit 분봉 백필 | TODO | |
| 10 | 모니터와 작업 스케줄러 | TODO | |
| 11 | 7일 연속 운영 검증 | TODO | |

---

## 1. Scaffold와 저장소
- **완료 조건:** 게이트 통과, CI 성공(run id 기록).
- **구현 파일**
  - scaffold 산출물: `pyproject.toml`, `uv.lock`, `src/quant_research/*`, `tests/*`, `CLAUDE.md`
  - `.github/workflows/ci.yml`: Actions SHA 고정, pip-audit 단계 추가
  - `docs/*`
- **실행 명령**
  - `python -I C:/dev/tools/scaffold/new_project.py quant-research --stack python`(`C:\dev`에서 실행)
  - 게이트: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q --cov`
- **진행 메모:** scaffold는 빈 폴더를 요구해서 `docs/`를 `C:\dev\qr-docs-staging`으로 옮겼다가 되돌렸습니다. 작업 폴더를 지우는 `rmdir`은 안전 검사가 막았고 필요하지도 않았습니다(scaffold는 빈 폴더를 허용).
- **검증 결과:** (작성 예정)
- **미해결 문제:** 없음
- **다음 작업:** 2. Phase 0 측정
