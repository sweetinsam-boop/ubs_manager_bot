import json
import os
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from storage.paths import get_path, PERFORMANCE_LOG_FILE

def generate_asset_chart(output_path="asset_chart.png"):
    """
    performance_log.json의 데이터를 읽어 포트폴리오 수익률과 벤치마크(VOO, QQQ)를 비교하는 그래프를 생성합니다.
    거래일이 아닌 날은 차트에서 제외합니다.
    """
    log_file = get_path(PERFORMANCE_LOG_FILE)
    
    if not os.path.exists(log_file):
        return False, "성과 데이터 파일이 존재하지 않습니다."
    
    try:
        with open(log_file, "r") as f:
            data = json.load(f)
            
        if len(data) < 2:
            return False, "데이터가 부족하여 그래프를 그릴 수 없습니다. (최소 2개 이상의 기록 필요)"
            
        df = pd.DataFrame(data)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
        
        # backfill된 데이터에서 benchmark/nasdaq가 None일 수 있으므로 제거
        df = df.dropna(subset=['equity', 'benchmark', 'nasdaq'])
        
        if len(df) < 2:
            return False, "데이터가 부족하여 그래프를 그릴 수 없습니다. (최소 2개 이상의 기록 필요)"

        # 1. 수익률 정규화 (첫 기록을 100으로 설정)
        first_equity = df['equity'].iloc[0]
        first_benchmark = df['benchmark'].iloc[0]
        first_nasdaq = df['nasdaq'].iloc[0]
        
        # 0 나누기 방지
        if first_equity == 0 or first_benchmark == 0 or first_nasdaq == 0:
            return False, "초기 데이터 값이 0이어서 수익률 계산이 불가능합니다."
            
        df['Portfolio'] = (df['equity'] / first_equity) * 100
        df['S&P500(VOO)'] = (df['benchmark'] / first_benchmark) * 100
        df['NASDAQ(QQQ)'] = (df['nasdaq'] / first_nasdaq) * 100
        
        # 2. 그래프 스타일 설정
        plt.style.use('dark_background') # 다크 모드 봇에 어울리는 배경
        fig, ax = plt.subplots(figsize=(10, 6), dpi=100)
        
        # 3. 비거래일 공백을 없애기 위해 '날짜 문자열'을 인덱스로 사용 (Discrete Plotting)
        # 단순히 x=date로 그리면 주말이 길게 벌어지므로, 인덱스 번호를 x로 쓰고 레이블만 날짜로 매핑합니다.
        dates_str = df['date'].dt.strftime('%m-%d').tolist()
        x_indices = range(len(df))
        
        ax.plot(x_indices, df['Portfolio'], label='My Portfolio', color='#FFD700', linewidth=2.5, marker='o', markersize=4) # Gold
        ax.plot(x_indices, df['S&P500(VOO)'], label='S&P 500 (VOO)', color='#00FA9A', linewidth=1.5, linestyle='--') # SpringGreen
        ax.plot(x_indices, df['NASDAQ(QQQ)'], label='NASDAQ (QQQ)', color='#1E90FF', linewidth=1.5, linestyle='--') # DodgerBlue
        
        # 4. 차트 레이아웃 및 디자인
        ax.set_title("Portfolio Growth vs Benchmarks (Base=100)", fontsize=15, pad=20, color='white')
        ax.set_xticks(x_indices)
        ax.set_xticklabels(dates_str, rotation=45, fontsize=9)
        
        # 가독성을 위해 x축 눈금 조절 (너무 많으면 띄엄띄엄 표시)
        if len(x_indices) > 15:
            for i, label in enumerate(ax.get_xticklabels()):
                if i % (len(x_indices) // 10) != 0:
                    label.set_visible(False)
        
        ax.legend(frameon=False, loc='upper left')
        ax.grid(True, linestyle=':', alpha=0.3)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        
        plt.tight_layout()
        plt.savefig(output_path)
        plt.close()
        
        return True, output_path
        
    except Exception as e:
        return False, f"그래프 생성 중 오류 발생: {str(e)}"

if __name__ == "__main__":
    # 테스트 실행
    success, result = generate_asset_chart("test_chart.png")
    print(f"Success: {success}, Result: {result}")
