# QA Tools

Holiday Robotics 품질·검증용 도구 모음입니다. 도구는 명령줄(`qa <도구> …`)로 먼저 만들고, 쓸 만해지면 창(QA Tools)의 화면으로 옮깁니다.

| 도구 | 명령줄 | 화면 | 설명서 |
|---|---|---|---|
| CAN Monitor (`canmon`) | 있음 | 있음 | [docs/canmon.md](docs/canmon.md), [docs/manual.html](docs/manual.html) |

2026-11-10에 측정용 노트북에서 실제 분석기(PCAN-USB Pro FD)의 채널 설정(로컬 CAN, 수신 전용)까지 확인했습니다. 로봇 버스에 연결해 프레임을 받아 본 적은 아직 없습니다. 그 밖의 검사는 가상 데이터와 가짜 장치로 했습니다.

## 설치

Python 3.10 이상이 필요합니다.

Ubuntu 23.04 이후(측정용 노트북의 24.04 포함)는 시스템 Python에 `pip install`을 막아 `externally-managed-environment` 오류가 납니다. 그래서 저장소 안에 가상환경(`.venv`)을 만들어 설치합니다.

```bash
git clone git@github.com:Holiday-Robot/QA-tools.git
cd QA-tools

# uv가 있으면 (2026-11-10 측정용 노트북에서 확인)
uv venv .venv
uv pip install --python .venv/bin/python -e ".[gui]"     # 화면 없이 명령줄만 쓸 때는 -e .

# uv가 없으면 (Ubuntu는 먼저 sudo apt install python3-venv)
python3 -m venv .venv
.venv/bin/pip install -e ".[gui]"
```

설치한 뒤에는 가상환경을 켜고 `qa`로 실행합니다. 켜지 않고 `.venv/bin/qa`로 실행해도 됩니다.

```bash
source .venv/bin/activate
qa list
```

설치하지 않고 저장소 폴더에서 `./qa`로 실행해도 됩니다. `./qa`는 그때 PATH에 있는 `python3`을 쓰므로, 가상환경을 켜 두었거나 `numpy`, `cantools`, `PyQt6`, `pyqtgraph`가 이미 설치되어 있어야 합니다.

## 사용

```bash
qa                       # 창 열기. 마지막에 쓴 도구가 열립니다
qa gui canmon capture.csv --map can0=HETC-L-TACT_ch0 --map can1=HETC-L-TACT_ch1
qa list                  # 도구 목록
qa canmon -h             # CAN Monitor 명령 목록

qa canmon sample sample_ltact.csv                                   # 가상 캡처 만들기
qa canmon analyze sample_ltact.csv --map can0=HETC-L-TACT_ch0       # 주기 분석 보고서
qa canmon view sample_ltact.csv --map can0=HETC-L-TACT_ch0          # CAN Monitor 창만 열기
```

`./install_shortcut.sh`는 Linux 앱 메뉴에 "QA Tools" 바로가기를 등록합니다.

## 폴더

```
qa                          설치 없이 실행하는 스크립트
pyproject.toml              의존성, qa 명령 등록
src/qatools/
  cli.py                    qa <도구> … 를 각 도구로 넘김
  registry.py               도구 목록 (TOOLS)
  shell/                    창: 도구 목록과 화면 전환, 글꼴·아이콘
  tools/
    canmon/
      core.py live.py tx.py sockcan.py hwconfig.py    계산과 통신. Qt를 쓰지 않음
      capture.py analyze.py sample.py cli.py          명령줄
      ui/                                             화면. 위 모듈만 부름
      hw/                                             CAN 채널 설정 스크립트
tests/                      검사 (화면 없이, CAN 장치 없이 실행)
data/dbc/                   채널별 DBC
data/hw_profiles.json       하드웨어 설정 공용 프로필 (FM26C 등)
docs/                       설명서와 화면 이미지
```

## 도구 추가하기

1. `src/qatools/tools/<이름>/`을 만들고 계산은 `core.py`에, 명령줄은 `cli.py`의 `main(argv)`에 둡니다. `core.py`는 Qt를 import하지 않습니다.
2. `src/qatools/registry.py`의 `TOOLS`에 한 줄을 추가합니다. 여기까지 하면 `qa <이름> …`으로 동작합니다.

   ```python
   Tool("mytool", "My Tool", "한 줄 설명", cli="qatools.tools.mytool.cli:main"),
   ```
3. 화면이 필요해지면 `ui/`에 `QWidget`(또는 `QMainWindow`)을 만들고 같은 줄에 `page="qatools.tools.mytool.ui.main:Page"`를 더합니다. 그러면 창의 도구 목록에 나타납니다. 화면은 `core.py`를 부르기만 합니다.
4. 검사는 `tests/<이름>/check_*.py`로 둡니다. 실패 내용을 출력하고 0이 아닌 값으로 끝나는 스크립트면 됩니다.

명령줄 도구는 결과를 파일(CSV, Markdown)로 남기고, 판정이 있으면 통과는 0, 실패는 0이 아닌 종료 코드로 돌려주도록 만듭니다. 시험 절차나 자동화에 그대로 넣기 위해서입니다.

화면에 아래 메서드가 있으면 창이 부릅니다. 둘 다 없어도 됩니다.

| 메서드 | 언제 |
|---|---|
| `shutdown()` | 창을 닫을 때. 스레드, 장치, 파일을 여기서 정리합니다. |
| `open_args(argv)` | `qa gui <이름> 인자…`로 실행했을 때 |

도구 화면은 처음 고를 때 만들어지고 그 뒤로 유지됩니다. 다른 도구를 보는 동안에도 측정은 계속됩니다. 도구 화면을 만들다 오류가 나면 그 자리에 오류 내용이 나오고 다른 도구는 그대로 쓸 수 있습니다.

## 검사

```bash
python3 tests/test_checks.py            # 전체
python3 tests/test_checks.py canmon     # 경로에 canmon이 들어간 것만
pytest                                  # pytest가 있으면 같은 검사를 하나씩
```

## DBC

`data/dbc/`의 DBC는 FM26 CAN 메시지 표에서 스크립트(`gen_dbc.py`)로 만든 파일을 복사한 것입니다. 그 스크립트와 메시지 표는 이 저장소에 없습니다. DBC를 다시 만들면 이 폴더의 파일도 바꿔 넣어야 합니다. 설계 주기와 프레임 길이 검사는 DBC의 `SubCodeCycles`, `SubCodeLengths` 속성을 씁니다.
