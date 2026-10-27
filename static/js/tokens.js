/* Страница API-токенов: клик по одноразовому секрету выделяет его
   и копирует в буфер обмена. Блока с секретом может не быть — тогда
   ничего не делаем (раньше это был Jinja-условный блок). */
(function () {
    const tokenNode = document.getElementById('newToken');
    if (!tokenNode) return;
    tokenNode.addEventListener('click', () => {
        const text = tokenNode.textContent;
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(tokenNode);
        selection.removeAllRanges();
        selection.addRange(range);
        navigator.clipboard?.writeText(text).catch(() => {});
    });
})();
