// ============================================================
// EMOJI
// ============================================================
const EMOJI_DATA = {
    smileys: [
        '😊', '😂', '❤️', '🔥', '👍', '💯', '🎉', '✨',
        '🤔', '😎', '💪', '🙏', '🥰', '😍', '🤗', '🫶',
        '😘', '😗', '😙', '🥲', '😅', '😆', '🤣', '🥹',
        '😌', '😏', '😒', '😔', '😕', '🙃', '🤑', '😲',
        '😳', '😱', '🤯', '🥳', '🤩', '😇', '🥺', '🤪',
        '😜', '😝', '🫠', '🤭', '🫣', '🤫', '🤥', '😶'
    ],
    gestures: [
        '👋', '🤚', '🖐️', '✋', '🖖', '👌', '🤌', '🤏',
        '✌️', '🤞', '🫰', '🤟', '🤘', '👈', '👉', '👆',
        '👇', '☝️', '👍', '👎', '👊', '✊', '🤛', '🤜',
        '👏', '🙌', '🫶', '🤲', '🤝', '🙏', '✍️', '💅'
    ],
    food: [
        '🍕', '🍔', '🌮', '🌯', '🥗', '🍣', '🍱', '🍜',
        '🍲', '🍛', '🍙', '🍚', '🍘', '🥟', '🍤', '🍗',
        '🥩', '🍖', '🥓', '🧀', '🥚', '🍳', '🥞', '🧇',
        '🥐', '🥖', '🍞', '🧈', '🧂', '🍿', '🧁', '🍰',
        '🎂', '🍪', '🍩', '🍫', '🍬', '🍭', '🍮', '☕',
        '🍵', '🧃', '🥤', '🧋', '🍺', '🍷', '🥂', '🍾'
    ],
    activities: [
        '🎉', '🎊', '🎁', '🎈', '🎆', '🎇', '✨', '🎯',
        '🎮', '🎲', '♟️', '🧩', '🎨', '🖌️', '🎭', '🎬',
        '🎤', '🎧', '🎼', '🎹', '🥁', '🎸', '🎺', '🎻',
        '📚', '📖', '✍️', '🧑‍💻', '🏃', '🚶', '🥾', '🚴',
        '🏕️', '🎣', '🧗', '🏊', '⚽', '🏀', '🏈', '⚾',
        '🎾', '🏐', '🏓', '🏸', '🥏', '🎱', '🏆', '🏅'
    ],
    travel: [
        '🚗', '🚕', '🚙', '🚌', '🚎', '🚐', '🛻', '🚚',
        '🚛', '🚜', '🏍️', '🛵', '🚲', '🛴', '🚁', '✈️',
        '🛩️', '🛫', '🛬', '🚀', '🚢', '⛵', '🚤', '🛶',
        '🚂', '🚆', '🚇', '🚉', '🏠', '🏡', '🏢', '🏥',
        '🏫', '🏭', '⛺', '🏕️', '🏔️', '⛰️', '🌋', '🏖️',
        '🏝️', '🌲', '🌳', '🗺️', '📍', '🧭', '🛣️', '🚧'
    ],
    objects: [
        '🔋', '🪫', '🔌', '💡', '🔦', '🕯️', '📡', '📶',
        '📱', '☎️', '☢️', '💻', '🖥️', '⌨️', '🖱️', '🖨️',
        '📷', '📹', '🎥', '📻', '🎙️', '🎧', '🔊', '🔔',
        '⌚', '⏰', '⏱️', '🧭', '⚙️', '🔧', '🪛', '🔩',
        '🔨', '🧰', '🪚', '⛏️', '🧲', '🔬', '🔭', '🛰️',
        '📊', '📈', '📉', '📋', '📁', '📂', '📄', '📝',
        '📌', '☣️', '✂️', '🔒', '🔓', '🔑', '🧯', '⚠️'
    ],
    weather: [
        '☀️', '🌤️', '⛅', '🌥️', '☁️', '🌦️', '🌧️', '⛈️',
        '🌩️', '🌨️', '❄️', '☃️', '⛄', '🌪️', '🌫️', '🌈',
        '🔥', '💧', '🌊', '🌡️', '🌬️', '💨', '⚡', '☔',
        '🌙', '🌛', '🌜', '🌚', '🌝', '⭐', '🌟', '💫',
        '🕐', '🕑', '🕒', '🕓', '🕔', '🕕', '🕖', '🕗',
        '🕘', '🕙', '🕚', '🕛', '⏰', '⏱️', '⏲️', '⌛'
    ]
};

let currentEmojiCategory = 'smileys';
let isEmojiPickerOpen = false;

function openEmojiPicker() {
    const picker = document.getElementById('emojiPicker');
    if (!picker) return;
    
    isEmojiPickerOpen = true;
    picker.style.display = 'flex';
    renderEmojiCategory(currentEmojiCategory);
    
    document.querySelectorAll('.emoji-cat-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.cat === currentEmojiCategory);
    });
}

function closeEmojiPicker() {
    const picker = document.getElementById('emojiPicker');
    if (picker) {
        picker.style.display = 'none';
    }
    isEmojiPickerOpen = false;
}

function toggleEmojiPicker() {
    if (isEmojiPickerOpen) {
        closeEmojiPicker();
    } else {
        openEmojiPicker();
    }
}

function renderEmojiCategory(category) {
    const grid = document.getElementById('emojiGrid');
    if (!grid) return;
    
    const emojis = EMOJI_DATA[category] || EMOJI_DATA.smileys;
    grid.innerHTML = emojis.map(emoji => 
        `<button class="emoji-item" data-emoji="${emoji}">${emoji}</button>`
    ).join('');
}

function insertEmoji(emoji) {
    const input = document.getElementById('messageInput');
    if (!input) return;
    
    const start = input.selectionStart;
    const end = input.selectionEnd;
    const text = input.value;
    
    input.value = text.substring(0, start) + emoji + text.substring(end);
    const newPos = start + emoji.length;
    input.selectionStart = input.selectionEnd = newPos;
    
    input.focus();
    closeEmojiPicker();
}

document.addEventListener('DOMContentLoaded', function() {
    const emojiBtn = document.getElementById('emojiBtn');
    if (emojiBtn) {
        emojiBtn.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            toggleEmojiPicker();
        });
    }
    
    const closeBtn = document.getElementById('emojiCloseBtn');
    if (closeBtn) {
        closeBtn.addEventListener('click', function() {
            closeEmojiPicker();
        });
    }
    
    document.querySelectorAll('.emoji-cat-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const cat = this.dataset.cat;
            currentEmojiCategory = cat;
            document.querySelectorAll('.emoji-cat-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            renderEmojiCategory(cat);
        });
    });
    
    document.getElementById('emojiGrid')?.addEventListener('click', function(e) {
        const item = e.target.closest('.emoji-item');
        if (item) {
            const emoji = item.dataset.emoji;
            if (emoji) {
                insertEmoji(emoji);
            }
        }
    });
    
    document.addEventListener('click', function(e) {
        const picker = document.getElementById('emojiPicker');
        const btn = document.getElementById('emojiBtn');
        if (isEmojiPickerOpen && picker && btn) {
            if (!picker.contains(e.target) && !btn.contains(e.target)) {
                closeEmojiPicker();
            }
        }
    });
    
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && isEmojiPickerOpen) {
            closeEmojiPicker();
        }
    });
    
    document.querySelector('.messages-container')?.addEventListener('scroll', function() {
        if (isEmojiPickerOpen) {
            closeEmojiPicker();
        }
    });
});

