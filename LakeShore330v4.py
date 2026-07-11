#!/usr/bin/env python3
# =========================================================================
# LakeShore330 온도 및 전압 로거 (Python 포트 - MATLAB LakeShore330v4.m과 동일 동작)
# 아키텍처: 영구 상태 머신, 내결함성 실시간 플로팅 및 동적 변화율 트래킹
#
# 의존성: pandas, matplotlib, openpyxl (xlsx 저장용)
#         pyvisa + VISA 백엔드 (USE_SIMULATION = False 로 실제 하드웨어 사용 시)
#         scipy (선택, CSV 기록 실패 시 EMERGENCY_DUMP.mat 저장용)
#   pip install pandas matplotlib openpyxl pyvisa scipy
# =========================================================================

import math
import os
import random
import time
from datetime import datetime, timedelta

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

# 한글 라벨/제목이 플롯에 정상적으로 렌더링되도록 시스템 폰트 지정 (없으면 기본 폰트로 대체)
_installed_fonts = {f.name for f in fm.fontManager.ttflist}
for _candidate in ('AppleGothic', 'Apple SD Gothic Neo', 'Malgun Gothic', 'NanumGothic'):
    if _candidate in _installed_fonts:
        plt.rcParams['font.family'] = _candidate
        break
plt.rcParams['axes.unicode_minus'] = False

# 1. 설정 플래그 (Configuration Flags)
USE_SIMULATION = True        # 실제 하드웨어 DAQ를 사용할 경우 False로 설정
LOOP_INTERVAL_SEC = 1.0      # 엄격한 샘플링 간격 목표 (초 단위, 가변 가능)
DIFF_WINDOW_SEC = 20.0       # 온도 차이를 계산할 윈도우 간격 (초 단위, 예: 15분 = 900)
BATCH_THRESHOLD = 30         # 디스크 I/O 전 메모리에 유지할 데이터 행(row)의 수


class LakeShore330DeadConnectionError(Exception):
    pass


class LakeShore330HardwareFailureError(Exception):
    pass


# =========================================================================
# 격리된 버퍼 매니저 (영구 메모리 계층)
# =========================================================================
class BufferManager:
    def __init__(self):
        self.buffer = []
        self.batch_limit = None
        self.target_file = None
        self.dynamic_col_name = None
        self.columns = None

    def init(self, batch_limit, target_file, window_str_eng):
        self.batch_limit = batch_limit
        self.target_file = target_file
        self.dynamic_col_name = f'dT_dt_{window_str_eng}'
        self.columns = ['Log_Number', 'Timestamp', 'Temperature_K', 'Sensor_V', self.dynamic_col_name]
        self.buffer = []

    def add(self, log_number, timestamp, temp_k, sensor_v, dT_dt):
        self.buffer.append({
            'Log_Number': log_number,
            'Timestamp': timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
            'Temperature_K': temp_k,
            'Sensor_V': sensor_v,
            self.dynamic_col_name: dT_dt,
        })

        if len(self.buffer) >= self.batch_limit:
            self._flush()

    def _flush(self):
        if not self.buffer:
            return
        batch_df = pd.DataFrame(self.buffer, columns=self.columns)
        write_header = not os.path.isfile(self.target_file)
        batch_df.to_csv(self.target_file, mode='a', header=write_header, index=False)
        self.buffer = []

    def rescue(self):
        print('\n\n--- 실험 종료 / 중단 신호 감지됨 ---')
        if self.target_file is None:
            return

        csv_write_success = False
        if self.buffer:
            try:
                self._flush()
                csv_write_success = True
            except Exception:
                rescue_df = pd.DataFrame(self.buffer, columns=self.columns)
                try:
                    from scipy.io import savemat
                    savemat('EMERGENCY_DUMP.mat', {col: rescue_df[col].to_numpy() for col in rescue_df.columns})
                except ImportError:
                    rescue_df.to_pickle('EMERGENCY_DUMP.pkl')
        else:
            if os.path.isfile(self.target_file):
                csv_write_success = True

        if csv_write_success:
            try:
                excel_file = self.target_file.replace('.csv', '.xlsx')
                final_data = pd.read_csv(self.target_file)
                final_data.to_excel(excel_file, index=False)
            except Exception:
                pass

        self.buffer = []
        self.batch_limit = None
        self.target_file = None


# =========================================================================
# 로컬 데이터 수집 함수 (분리된 계층)
# =========================================================================
def read_data_real(instr):
    try:
        instr.write('CDAT?')
        temp_k = float(instr.read())
        instr.write('SDAT?')
        sensor_v = float(instr.read())
    except Exception:
        temp_k = float('nan')
        sensor_v = float('nan')
    return temp_k, sensor_v


def read_data_mock(elapsed_time):
    T_start = 300.0
    T_base = 16.0
    tau = 1800.0

    ideal_temp = (T_start - T_base) * math.exp(-elapsed_time / tau) + T_base
    thermal_noise = random.gauss(0, 1) * (0.05 + random.random() * 0.05)
    temp_k = ideal_temp + thermal_noise

    ideal_voltage = (-0.00176 * temp_k) + 1.02816
    electrical_noise = random.gauss(0, 1) * 0.0001
    sensor_v = ideal_voltage + electrical_noise

    return temp_k, sensor_v


# =========================================================================
# 하드웨어 초기화 (안전한 핸드셰이크 및 디스플레이 듀얼 라우팅)
# =========================================================================
def init_hardware():
    import pyvisa
    rm = pyvisa.ResourceManager()
    instr = rm.open_resource('GPIB0::12::INSTR')
    instr.timeout = 3000  # ms

    # 초기 검증 핸드셰이크 (Verification Handshake)
    instr.write('CDAT?')
    test_resp = instr.read().strip()
    try:
        valid = not math.isnan(float(test_resp))
    except ValueError:
        valid = False
    if not test_resp or not valid:
        raise LakeShore330DeadConnectionError('장비가 연결되었으나 유효하지 않은 데이터를 반환했습니다.')

    # 주 센서(Channel A 기준)를 두 디스플레이에 동시 할당
    instr.write('CCHN A')
    instr.write('CUNI K')
    time.sleep(1.5)  # 장비 내부 업데이트 사이클 대기 (매뉴얼 필수 권장사항)

    instr.write('SCHN A')
    instr.write('SUNI S')
    time.sleep(1.5)

    return instr


# =========================================================================
# 동적 파일명 및 UI/데이터 라벨 문자열 초기화
# =========================================================================
def build_window_strings(diff_window_sec):
    # 모듈로(mod) 연산을 통해 초 단위가 60의 배수인지 확인하여 UI 문자열 최적화
    if diff_window_sec % 60 == 0:
        minutes = int(diff_window_sec // 60)
        window_str_kor = f'{minutes}분'
        window_str_eng = f'{minutes}min'
    else:
        window_str_kor = f'{diff_window_sec:g}초'
        window_str_eng = f'{diff_window_sec:g}sec'
    return window_str_kor, window_str_eng


# =========================================================================
# 통합 정리 매니저 (Unified Cleanup Manager)
# =========================================================================
def system_cleanup(instr, fig, target_file, window_str_kor, window_str_eng, buffer_manager):
    buffer_manager.rescue()
    plot_filename = target_file.replace('.csv', '.png')
    dynamic_col_name = f'dT_dt_{window_str_eng}'

    fig_open = fig is not None and plt.fignum_exists(fig.number)

    if fig_open:
        print('[정보] 열려있는 피겨에서 고해상도 듀얼 플롯 이미지를 추출합니다...')
        try:
            fig.savefig(plot_filename, dpi=300)
            print(f'[성공] 최종 플롯 저장 완료: {plot_filename}')
        except Exception as e:
            print(f'[경고] 플롯 이미지 저장에 실패했습니다: {e}')
    else:
        print('[정보] 플롯 창이 닫혀있어 백그라운드에서 이중 플롯을 재생성하여 저장합니다...')
        try:
            if os.path.isfile(target_file):
                df = pd.read_csv(target_file, parse_dates=['Timestamp'])
                if len(df) > 0:
                    headless_fig = Figure(figsize=(8, 6))
                    FigureCanvasAgg(headless_fig)
                    ax1 = headless_fig.add_subplot(2, 1, 1)
                    ax2 = headless_fig.add_subplot(2, 1, 2, sharex=ax1)

                    ax1.plot(df['Timestamp'], df['Temperature_K'], '-o',
                              color=(0.8500, 0.3250, 0.0980), linewidth=1.5, markerfacecolor='r')
                    ax1.set_title('실시간 절대 온도 (Absolute Temperature)')
                    ax1.set_ylabel('온도 (K)')
                    ax1.grid(True)

                    ax2.plot(df['Timestamp'], df[dynamic_col_name], '-o',
                              color=(0.0, 0.4470, 0.7410), linewidth=1.5, markerfacecolor='b')
                    ax2.set_title(f'{window_str_kor} 온도 차이 (ΔT_{{{window_str_eng}}})')
                    ax2.set_xlabel('시간')
                    ax2.set_ylabel('ΔT (K)')
                    ax2.grid(True)

                    headless_fig.tight_layout()
                    headless_fig.savefig(plot_filename, dpi=300)
                    print(f'[성공] 백그라운드 듀얼 플롯 생성 및 저장 완료: {plot_filename}')
        except Exception as e:
            print(f'[경고] 백그라운드 플롯 복구 및 저장에 실패했습니다: {e}')

    if instr is not None:
        try:
            instr.close()
        except Exception:
            pass


# =========================================================================
# 메인 로깅 루프
# =========================================================================
def main():
    # 2. 동적 파일명 및 UI/데이터 라벨 문자열 초기화
    timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'TempLog_{timestamp_str}.csv'
    window_str_kor, window_str_eng = build_window_strings(DIFF_WINDOW_SEC)

    # 3. 안전한 핸드셰이크 및 디스플레이 듀얼 라우팅을 통한 하드웨어 초기화
    instr = None
    if not USE_SIMULATION:
        try:
            instr = init_hardware()
        except Exception as e:
            raise LakeShore330HardwareFailureError(
                f'LakeShore 330 연결 및 초기화에 실패했습니다.\n{e}'
            ) from e
        hw_status = '실제 하드웨어 (LakeShore 330 듀얼 라우팅 완료)'
    else:
        hw_status = '시뮬레이션 모드 (가상 데이터)'

    # 4. 절대적인 데이터 및 하드웨어 보호 (버퍼 매니저 초기화 - 동적 컬럼명 전달)
    buffer_manager = BufferManager()
    buffer_manager.init(BATCH_THRESHOLD, filename, window_str_eng)

    # 5. 터미널 UI 및 그래픽 플롯 초기화
    print('\n=======================================================================')
    print('                 LakeShore330 DAQ 시스템 시작됨 (v4.2)               ')
    print('=======================================================================')
    print(f'대상 파일 : {filename}')
    print(f'하드웨어  : {hw_status}')
    print(f'윈도우 간격: {window_str_kor} ({DIFF_WINDOW_SEC:g} 초)')
    print('-----------------------------------------------------------------------')
    print(f"{'로그 #':<8} | {'타임스탬프':<23} | {'온도 (K)':<10} | {'전압 (V)':<10} | {window_str_kor} 변화율(K)")
    print('-----------------------------------------------------------------------')

    plt.ion()
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(8, 6))
    try:
        fig.canvas.manager.set_window_title('LakeShore330 실시간 모니터')
    except Exception:
        pass

    live_plot1, = ax1.plot([], [], '-o', color=(0.8500, 0.3250, 0.0980), linewidth=1.5, markerfacecolor='r')
    ax1.set_title('실시간 절대 온도 (Absolute Temperature)')
    ax1.set_ylabel('온도 (K)')
    ax1.grid(True)

    live_plot2, = ax2.plot([], [], '-o', color=(0.0, 0.4470, 0.7410), linewidth=1.5, markerfacecolor='b')
    ax2.set_title(f'{window_str_kor} 온도 차이 (ΔT_{{{window_str_eng}}})')
    ax2.set_xlabel('시간')
    ax2.set_ylabel('ΔT (K)')
    ax2.grid(True)

    fig.tight_layout()

    time_data = []
    temp_data = []
    rate_data = []
    window_ptr = 0  # 동적 이동 윈도우 포인터 (단조 증가, O(1) 상각 탐색용)

    log_counter = 0
    global_start_time = time.perf_counter()

    # 6. 메인 로깅 루프
    try:
        while True:
            loop_start_time = time.perf_counter()
            log_counter += 1

            current_datetime = datetime.now()
            elapsed_total_sec = time.perf_counter() - global_start_time

            if USE_SIMULATION:
                temp_k, sensor_v = read_data_mock(elapsed_total_sec)
            else:
                temp_k, sensor_v = read_data_real(instr)

            # =============================================================
            # 동적 이동 윈도우 기반 변화율 산출
            # =============================================================
            time_data.append(current_datetime)
            temp_data.append(temp_k)

            target_time = current_datetime - timedelta(seconds=DIFF_WINDOW_SEC)

            if (current_datetime - time_data[0]).total_seconds() < DIFF_WINDOW_SEC:
                dT_dt = float('nan')  # 윈도우 구간이 채워지지 않은 상태
            else:
                # 타임스탬프가 단조 증가하므로 포인터는 항상 전진만 하면 됨 (상각 O(1))
                while time_data[window_ptr] < target_time:
                    window_ptr += 1
                dT_dt = temp_k - temp_data[window_ptr]

            rate_data.append(dT_dt)
            # =============================================================

            current_str = current_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            print(f'{log_counter:<10d} | {current_str:<23s} | {temp_k:<10.4f} | {sensor_v:<10.6f} | {dT_dt:<15.4f}')

            # 동적 변수도 순차적으로 버퍼에 푸시됨
            buffer_manager.add(log_counter, current_datetime, temp_k, sensor_v, dT_dt)

            if plt.fignum_exists(fig.number):
                live_plot1.set_data(time_data, temp_data)
                live_plot2.set_data(time_data, rate_data)

                if log_counter == 1 or log_counter % 10 == 0:
                    ax1.set_xlim(time_data[0], time_data[-1] + timedelta(seconds=LOOP_INTERVAL_SEC))
                    ax1.relim()
                    ax1.autoscale_view(scalex=False)
                    ax2.relim()
                    ax2.autoscale_view(scalex=False)
                    plt.pause(0.001)

            execution_time = time.perf_counter() - loop_start_time
            time_remaining = max(0.0, LOOP_INTERVAL_SEC - execution_time)
            time.sleep(time_remaining)
    finally:
        system_cleanup(instr, fig, filename, window_str_kor, window_str_eng, buffer_manager)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n[정보] 사용자에 의해 중단되었습니다 (Ctrl+C).')
