import re
import os

path = '/Users/song/Documents/CODING/무매코딩_NAS/telegram_bot.py'
with open(path, 'rb') as f:
    orig_text = f.read().decode('utf-8', 'replace')

lines = orig_text.splitlines()
new_lines = []
imported = set()

# 1. 중복 임포트 라인 제거
for line in lines:
    strip_l = line.strip()
    if strip_l.startswith(('from notification_service', 'from ui_formatter', 'from trade_service')):
        if strip_l not in imported:
            new_lines.append(line)
            imported.add(strip_l)
        else:
            continue # 중복 제거
    else:
        new_lines.append(line)

text = '\n'.join(new_lines)

# 2. 마크다운 파라미터 및 기호 제거 (sed 잔여물 포함)
text = text.replace('**', '').replace('_', '').replace('`', '')
text = text.replace(', parse_mode=None', '').replace(', parse_mode=None', '')
text = text.replace('parse_mode=None, ', '').replace('parse_mode=None', '')

# 3. 구문 오류 (쉼표 중복 등) 교정
text = text.replace(', ,', ',').replace('(,', '(').replace(',)', ')')
text = text.replace(', ,', ',') # 한 번 더

# 4. 주요 핸들러의 reply_text를 UIFormatter.clean_text로 감싸기 (선택적 신중 적용)
# 간단한 패턴 매칭으로 감싸줍니다. 예: reply_text(msg) -> reply_text(UIFormatter.clean_text(msg))
targets = ['welcome_msg', 'help_text', 'msg', 'sync_done', 'summary', 'verify_msg', 'res_msg']
for t in targets:
    # 이미 반영된 경우 제외하고 감싸기 시도
    pattern = rf'reply_text\({t}(?!\))'
    text = re.sub(pattern, f'reply_text(UIFormatter.clean_text({t})', text)

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)

print("Telegram bot cleanup script finished successfully.")
