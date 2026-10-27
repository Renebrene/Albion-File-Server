/* Клиентский код публичной страницы папки: ленивая подгрузка файлов.
   Подключается только на /d/<folder_ref>; параметры Jinja не нужны —
   следующий URL приходит в data-атрибуте #publicLazyLoader. */
const publicLoader = document.getElementById('publicLazyLoader');
const publicStatus = document.getElementById('publicLazyStatus');
const publicButton = document.getElementById('publicLoadMore');
let publicNextUrl = publicLoader ? (publicLoader.dataset.nextUrl || '') : '';
let publicLoading = false;

async function publicLoadNextPage() {
    if (!publicNextUrl || publicLoading) return;
    publicLoading = true;
    publicButton.disabled = true;
    publicStatus.textContent = 'Загрузка…';
    try {
        const response = await fetch(publicNextUrl);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        const target = document.getElementById('publicItems');
        doc.querySelectorAll('#publicItems .item').forEach(node => {
            target.appendChild(document.importNode(node, true));
        });
        const nextLoader = doc.getElementById('publicLazyLoader');
        publicNextUrl = nextLoader ? (nextLoader.dataset.nextUrl || '') : '';
        publicLoader.dataset.nextUrl = publicNextUrl;
        if (!publicNextUrl) {
            publicLoader.hidden = true;
            publicStatus.textContent = '';
        } else {
            publicStatus.textContent = '';
        }
    } catch (err) {
        console.error('Ошибка публичной lazy-загрузки:', err);
        publicStatus.textContent = 'Не удалось загрузить ещё файлы';
    } finally {
        publicLoading = false;
        publicButton.disabled = false;
    }
}

if (publicNextUrl && 'IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
        if (entries.some(entry => entry.isIntersecting)) publicLoadNextPage();
    }, { rootMargin: '400px' });
    observer.observe(publicLoader);
} else if (publicNextUrl) {
    publicStatus.textContent = 'Прокрутите вниз или нажмите кнопку';
}
