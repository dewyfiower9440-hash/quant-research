# 기술적 의사결정 기록 (ADR)

형식: 번호, 날짜, 상태(제안/채택/대체됨), 맥락, 선택지, 결정, 근거, 영향.
설계 수준의 결정은 `docs/plans/2026-11-08-autonomous-quant-research.md`(v2.1)에 있습니다. 여기에는 구현 중 내린 결정만 기록합니다.

## ADR-001 (2026-11-08, 채택) 프로젝트 생성 방식
- **맥락:** 설계 문서가 이미 `C:\dev\quant-research\docs`에 있었고, scaffold는 비어 있지 않은 폴더를 거부합니다.
- **선택지**
  - (a) scaffold 코드를 수정한다
  - (b) 다른 이름으로 생성한 뒤 옮긴다
  - (c) `docs/`를 잠시 옮기고 빈 폴더에 생성한다
- **결정:** (c)
- **근거:** 공유 도구(dev-tools)를 수정하지 않고, 경로도 그대로 유지할 수 있습니다.
- **영향:** 없음. 옮긴 뒤 원위치로 되돌렸습니다.

## ADR-002 (2026-11-08, 채택) CI 보강
- **결정:** GitHub Actions를 커밋 SHA로 고정합니다(`actions/checkout` v5 = fbc6f39…, `astral-sh/setup-uv` v7 = 37802ad…). `uv export` 결과에 `uvx pip-audit` 단계를 추가합니다.
- **근거:** 설계 v2.1 §4 공통 사항(공급망)
- **영향:** 버전을 올릴 때는 SHA와 주석의 태그를 함께 바꿔야 합니다. dev-tools Gotchas에 따르면 Dependabot이 이 workflow를 갱신하지 못할 수 있으므로, 수동으로 확인합니다.
