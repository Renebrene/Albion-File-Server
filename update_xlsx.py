import re

# Новая функция export_xlsx
new_function = '''@app.route('/export_xlsx')
def export_xlsx():
    """Экспорт в XLSX: папка, физическое имя и ссылка (с кириллицей)"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Список файлов"
    
    # 3 колонки
    ws.append(["Папка", "Физическое имя", "Ссылка"])

    # Стилизация заголовка
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")

    folders = load_folders()
    db = load_db()
    
    for internal_id, record in db.items():
        # Путь к папке
        folder_path = '/'.join(get_folder_path(record.get('folder_id'), folders)) if record.get('folder_id') else 'Корень'
        physical_path = record.get('physical_path', record['current_filename'])
        # Ссылка с нормальной кириллицей (без URL-кодирования)
        download_url = f"{BASE_URL}/{physical_path}"
        
        ws.append([
            folder_path,
            record['current_filename'],
            download_url
        ])

    # Автоширина колонок
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 2, 80)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    update_last_export()

    return send_file(output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='file_list.xlsx')'''

# Читаем файл
with open('server.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Находим и заменяем функцию export_xlsx
pattern = r"@app\.route\('/export_xlsx'\)\s+def export_xlsx\(\):.*?(?=\n@app\.route|\nif __name__|$)"
replacement = new_function + '\n\n'

new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

# Проверяем, что замена произошла
if new_content == content:
    print("❌ Функция не найдена или не заменена")
else:
    # Сохраняем файл
    with open('server.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("✅ Функция export_xlsx обновлена")
    print("🔄 Перезапуск сервиса...")
    
    import subprocess
    subprocess.run(['sudo', 'systemctl', 'restart', 'albion-files'])
    print("✅ Сервис перезапущен")
