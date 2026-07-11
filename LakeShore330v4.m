function LakeShore330v4()
    % =========================================================================
    % LakeShore330 온도 및 전압 로거 (v4.2 - 동적 차분 윈도우 파라미터화 적용)
    % 아키텍처: 영구 상태 머신, 내결함성 실시간 플로팅 및 동적 변화율 트래킹
    % =========================================================================

    % 1. 설정 플래그 (Configuration Flags)
    USE_SIMULATION = true;       % 실제 하드웨어 DAQ를 사용할 경우 false로 설정
    LOOP_INTERVAL_SEC = 1.0;     % 엄격한 샘플링 간격 목표 (초 단위, 가변 가능)
    DIFF_WINDOW_SEC = 20.0;      % 온도 차이를 계산할 윈도우 간격 (초 단위, 예: 15분 = 900)
    BATCH_THRESHOLD = 30;        % 디스크 I/O 전 메모리에 유지할 데이터 행(row)의 수

    % 2. 동적 파일명 및 UI/데이터 라벨 문자열 초기화
    timestampStr = char(datetime('now', 'Format', 'yyyyMMdd_HHmmss'));
    filename = sprintf('TempLog_%s.csv', timestampStr); 
    
    % 모듈로(mod) 연산을 통해 초 단위가 60의 배수인지 확인하여 UI 문자열 최적화
    if mod(DIFF_WINDOW_SEC, 60) == 0
        windowStrKor = sprintf('%d분', DIFF_WINDOW_SEC / 60);
        windowStrEng = sprintf('%dmin', DIFF_WINDOW_SEC / 60);
    else
        windowStrKor = sprintf('%g초', DIFF_WINDOW_SEC);
        windowStrEng = sprintf('%gsec', DIFF_WINDOW_SEC);
    end
    
    % 3. 안전한 핸드셰이크 및 디스플레이 듀얼 라우팅을 통한 하드웨어 초기화
    visaObj = [];
    if ~USE_SIMULATION
        try
            visaObj = visadev('GPIB0::12::INSTR');
            visaObj.Timeout = 3.0; 
            
            % 초기 검증 핸드셰이크 (Verification Handshake)
            writeline(visaObj, 'CDAT?');
            testResp = readline(visaObj);
            if isempty(testResp) || isnan(str2double(testResp))
                error('LakeShore330:DeadConnection', '장비가 연결되었으나 유효하지 않은 데이터를 반환했습니다.');
            end
            
            % 주 센서(Channel A 기준)를 두 디스플레이에 동시 할당
            writeline(visaObj, 'CCHN A'); 
            writeline(visaObj, 'CUNI K'); 
            
            pause(1.5); 
            
            writeline(visaObj, 'SCHN A'); 
            writeline(visaObj, 'SUNI S'); 
            
            pause(1.5); 
            
        catch ME
            error('LakeShore330:HardwareFailure', ...
                  'LakeShore 330 연결 및 초기화에 실패했습니다.\n%s', ME.message);
        end
        hwStatus = '실제 하드웨어 (LakeShore 330 듀얼 라우팅 완료)';
    else
        hwStatus = '시뮬레이션 모드 (가상 데이터)';
    end

    % 4. 절대적인 데이터 및 하드웨어 보호 (버퍼 매니저 초기화 - 동적 컬럼명 전달)
    BufferManager('init', BATCH_THRESHOLD, filename, windowStrEng, [], []);

    % 5. 터미널 UI 및 그래픽 플롯 초기화
    fprintf('\n=======================================================================\n');
    fprintf('                 LakeShore330 DAQ 시스템 시작됨 (v4.2)               \n');
    fprintf('=======================================================================\n');
    fprintf('대상 파일 : %s\n', filename);
    fprintf('하드웨어  : %s\n', hwStatus);
    fprintf('윈도우 간격: %s (%g 초)\n', windowStrKor, DIFF_WINDOW_SEC);
    fprintf('-----------------------------------------------------------------------\n');
    fprintf('%-8s | %-23s | %-10s | %-10s | %s 변화율(K)\n', '로그 #', '타임스탬프', '온도 (K)', '전압 (V)', windowStrKor);
    fprintf('-----------------------------------------------------------------------\n');

    fig = figure('Name', 'LakeShore330 실시간 모니터', 'NumberTitle', 'off', 'Position', [100, 100, 800, 600]);
    tlo = tiledlayout(fig, 2, 1, 'TileSpacing', 'compact', 'Padding', 'compact');
    
    ax1 = nexttile(tlo);
    livePlot1 = plot(ax1, NaT, NaN, '-o', 'Color', [0.8500 0.3250 0.0980], ...
                    'LineWidth', 1.5, 'MarkerFaceColor', 'r');
    title(ax1, '실시간 절대 온도 (Absolute Temperature)');
    ylabel(ax1, '온도 (K)');
    grid(ax1, 'on');
    
    ax2 = nexttile(tlo);
    livePlot2 = plot(ax2, NaT, NaN, '-o', 'Color', [0.0, 0.4470, 0.7410], ...
                    'LineWidth', 1.5, 'MarkerFaceColor', 'b');
    title(ax2, sprintf('%s 온도 차이 (\\DeltaT_{%s})', windowStrKor, windowStrEng));
    xlabel(ax2, '시간');
    ylabel(ax2, '\DeltaT (K)');
    grid(ax2, 'on');

    linkaxes([ax1, ax2], 'x');
    
    timeData = datetime.empty;
    tempData = [];
    rateData = [];
    windowPtr = 1; % 동적 이동 윈도우 포인터 (단조 증가, O(1) 상각 탐색용)

    % Cleanup 함수에 동적 문자열 데이터 전달
    cleanupObj = onCleanup(@() systemCleanup(visaObj, fig, filename, windowStrKor, windowStrEng));

    isLogging = true;
    logCounter = 0;
    globalStartTime = tic;

    % 6. 메인 로깅 루프
    while isLogging
        loopStartTime = tic;
        logCounter = logCounter + 1;
        
        currentDatetime = datetime('now'); 
        elapsedTotalSec = toc(globalStartTime);

        if USE_SIMULATION
            [tempK, sensorV] = readDataMock(elapsedTotalSec);
        else
            [tempK, sensorV] = readDataReal(visaObj);
        end

        % =====================================================================
        % 동적 이동 윈도우 기반 변화율 산출
        % =====================================================================
        timeData(end+1) = currentDatetime;
        tempData(end+1) = tempK;
        
        targetTime = currentDatetime - seconds(DIFF_WINDOW_SEC);
        
        if (currentDatetime - timeData(1)) < seconds(DIFF_WINDOW_SEC)
            dT_dt = NaN; % 윈도우 구간이 채워지지 않은 상태
        else
            % 타임스탬프가 단조 증가하므로 포인터는 항상 전진만 하면 됨 (상각 O(1))
            while timeData(windowPtr) < targetTime
                windowPtr = windowPtr + 1;
            end
            T_past = tempData(windowPtr);
            dT_dt = tempK - T_past;
        end
        
        rateData(end+1) = dT_dt;
        % =====================================================================

        currentStr = char(currentDatetime, 'yyyy-MM-dd HH:mm:ss.SSS');
        
        fprintf('%-10d | %-23s | %-10.4f | %-10.6f | %-15.4f\n', logCounter, currentStr, tempK, sensorV, dT_dt);

        % 동적 변수도 순차적으로 버퍼에 푸시됨
        BufferManager('add', logCounter, currentDatetime, tempK, sensorV, dT_dt);

        if isvalid(fig) && isvalid(livePlot1) && isvalid(livePlot2)
            livePlot1.XData = timeData;
            livePlot1.YData = tempData;
            
            livePlot2.XData = timeData;
            livePlot2.YData = rateData;
            
            if logCounter == 1 || mod(logCounter, 10) == 0
                ax1.XLim = [timeData(1), timeData(end) + seconds(LOOP_INTERVAL_SEC)];
                drawnow; 
            end
        end

        executionTime = toc(loopStartTime);
        timeRemaining = max(0, LOOP_INTERVAL_SEC - executionTime);
        pause(timeRemaining);
    end
end


% =========================================================================
% 통합 정리 매니저 (Unified Cleanup Manager)
% =========================================================================
function systemCleanup(visaObj, fig, targetFile, windowStrKor, windowStrEng)
    BufferManager('rescue', [], [], [], [], []);
    plotFilename = strrep(targetFile, '.csv', '.png'); 
    dynamicColName = sprintf('dT_dt_%s', windowStrEng); % 동적 컬럼명 생성

    if ~isempty(fig) && isvalid(fig)
        fprintf('[정보] 열려있는 피겨에서 고해상도 듀얼 플롯 이미지를 추출합니다...\n');
        try
            exportgraphics(fig, plotFilename, 'Resolution', 300);
            fprintf('[성공] 최종 플롯 저장 완료: %s\n', plotFilename);
        catch ME
            fprintf('[경고] 플롯 이미지 저장에 실패했습니다: %s\n', ME.message);
        end
    else
        fprintf('[정보] 플롯 창이 닫혀있어 백그라운드에서 이중 플롯을 재생성하여 저장합니다...\n');
        try
            if isfile(targetFile)
                opts = detectImportOptions(targetFile);
                if ismember('Timestamp', opts.VariableNames)
                    opts = setvartype(opts, 'Timestamp', 'datetime');
                    opts = setvaropts(opts, 'Timestamp', 'InputFormat', 'yyyy-MM-dd HH:mm:ss.SSS');
                end
                df = readtable(targetFile, opts);
                
                if height(df) > 0
                    tempFig = figure('Visible', 'off', 'Position', [100, 100, 800, 600]);
                    tempTlo = tiledlayout(tempFig, 2, 1, 'TileSpacing', 'compact', 'Padding', 'compact');
                    
                    tempAx1 = nexttile(tempTlo);
                    plot(tempAx1, df.Timestamp, df.Temperature_K, '-o', 'Color', [0.8500 0.3250 0.0980], ...
                         'LineWidth', 1.5, 'MarkerFaceColor', 'r');
                    title(tempAx1, '실시간 절대 온도 (Absolute Temperature)');
                    ylabel(tempAx1, '온도 (K)');
                    grid(tempAx1, 'on');
                    
                    tempAx2 = nexttile(tempTlo);
                    % 동적 필드 참조 문법 df.(variableName) 사용
                    plot(tempAx2, df.Timestamp, df.(dynamicColName), '-o', 'Color', [0.0, 0.4470, 0.7410], ...
                         'LineWidth', 1.5, 'MarkerFaceColor', 'b');
                    title(tempAx2, sprintf('%s 온도 차이 (\\DeltaT_{%s})', windowStrKor, windowStrEng));
                    xlabel(tempAx2, '시간');
                    ylabel(tempAx2, '\DeltaT (K)');
                    grid(tempAx2, 'on');
                    
                    linkaxes([tempAx1, tempAx2], 'x');
                    
                    exportgraphics(tempFig, plotFilename, 'Resolution', 300);
                    close(tempFig);
                    fprintf('[성공] 백그라운드 듀얼 플롯 생성 및 저장 완료: %s\n', plotFilename);
                end
            end
        catch ME
            fprintf('[경고] 백그라운드 플롯 복구 및 저장에 실패했습니다: %s\n', ME.message);
            if exist('tempFig', 'var') && isvalid(tempFig), close(tempFig); end
        end
    end

    if ~isempty(visaObj) && isvalid(visaObj)
        try delete(visaObj); catch; end
    end
end


% =========================================================================
% 격리된 버퍼 매니저 (영구 메모리 계층)
% =========================================================================
function BufferManager(action, arg1, arg2, arg3, arg4, arg5)
    persistent dataBuffer bIndex batchLimit targetFile

    switch action
        case 'init'
            batchLimit = arg1;
            targetFile = arg2;
            windowStrEng = arg3; % 파라미터화된 문자열 수신
            bIndex = 0;
            
            % 동적 컬럼 이름 생성
            dynamicColName = sprintf('dT_dt_%s', windowStrEng);
            varNames = {'Log_Number', 'Timestamp', 'Temperature_K', 'Sensor_V', dynamicColName};
            varTypes = {'double', 'datetime', 'double', 'double', 'double'};
            dataBuffer = table('Size', [batchLimit, 5], ...
                               'VariableTypes', varTypes, ...
                               'VariableNames', varNames);
            dataBuffer.Timestamp.Format = 'yyyy-MM-dd HH:mm:ss.SSS';

        case 'add'
            bIndex = bIndex + 1;
            % 인덱스를 사용한 동적 할당 보장
            dataBuffer{bIndex, 1} = arg1;
            dataBuffer{bIndex, 2} = arg2;
            dataBuffer{bIndex, 3} = arg3;
            dataBuffer{bIndex, 4} = arg4;
            dataBuffer{bIndex, 5} = arg5;

            if bIndex >= batchLimit
                batchData = dataBuffer(1:bIndex, :);
                if isfile(targetFile)
                    writetable(batchData, targetFile, 'WriteMode', 'append');
                else
                    writetable(batchData, targetFile); 
                end
                bIndex = 0; 
            end

        case 'rescue'
            fprintf('\n\n--- 실험 종료 / 중단 신호 감지됨 ---\n');
            if isempty(targetFile), return; end

            csvWriteSuccess = false;
            if bIndex > 0
                rescueTable = dataBuffer(1:bIndex, :);
                try
                    if isfile(targetFile)
                        writetable(rescueTable, targetFile, 'WriteMode', 'append');
                    else
                        writetable(rescueTable, targetFile);
                    end
                    csvWriteSuccess = true;
                catch ME
                    save('EMERGENCY_DUMP.mat', 'rescueTable');
                end
            else
                if isfile(targetFile), csvWriteSuccess = true; end
            end

            if csvWriteSuccess
                try
                    excelFile = strrep(targetFile, '.csv', '.xlsx');
                    opts = detectImportOptions(targetFile);
                    if ismember('Timestamp', opts.VariableNames)
                        opts = setvartype(opts, 'Timestamp', 'string');
                    end
                    finalData = readtable(targetFile, opts);
                    writetable(finalData, excelFile);
                catch
                end
            end
            
            dataBuffer = []; bIndex = []; batchLimit = []; targetFile = [];
    end
end


% =========================================================================
% 로컬 데이터 수집 함수 (분리된 계층)
% =========================================================================
function [tempK, sensorV] = readDataReal(visaObj)
    try
        writeline(visaObj, 'CDAT?');
        tempK = str2double(readline(visaObj));
        writeline(visaObj, 'SDAT?');
        sensorV = str2double(readline(visaObj));
    catch
        tempK = NaN; 
        sensorV = NaN;
    end
end

function [tempK, sensorV] = readDataMock(elapsedTime)
    T_start = 300.0;
    T_base  = 16.0;
    tau     = 1800.0; 
    
    idealTemp = (T_start - T_base) * exp(-elapsedTime / tau) + T_base;
    thermalNoise = randn() * (0.05 + (rand() * 0.05));
    tempK = idealTemp + thermalNoise;
    
    idealVoltage = (-0.00176 * tempK) + 1.02816;
    electricalNoise = randn() * 0.0001; 
    sensorV = idealVoltage + electricalNoise;
end