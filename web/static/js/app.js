// Stock Trading Chatbot - Frontend JavaScript

const API_BASE = '';  // Use relative URLs for same-origin requests

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadStockWatchlist();
    loadChart();
    loadPredictions();
    loadAccuracy();

    // Enter key in chat input
    document.getElementById('chatInput').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            sendMessage();
        }
    });
});

// Chatbot Functions
async function sendMessage() {
    const input = document.getElementById('chatInput');
    const query = input.value.trim();

    if (!query) return;

    // Add user message to chat
    addMessage(query, 'user');
    input.value = '';

    // Show typing indicator
    const typingDiv = document.createElement('div');
    typingDiv.className = 'bot-message typing-indicator';
    typingDiv.innerHTML = '<div class="message-content">Thinking...</div>';
    document.getElementById('chatMessages').appendChild(typingDiv);
    scrollToBottom();

    try {
        const response = await fetch(`${API_BASE}/api/chatbot`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ query: query })
        });

        const data = await response.json();

        // Remove typing indicator
        typingDiv.remove();

        // Add bot response
        addMessage(data.response, 'bot');

    } catch (error) {
        typingDiv.remove();
        addMessage('Sorry, I encountered an error. Please try again.', 'bot');
        console.error('Error:', error);
    }
}

function addMessage(text, type) {
    const messagesDiv = document.getElementById('chatMessages');
    const messageDiv = document.createElement('div');
    messageDiv.className = type === 'user' ? 'user-message' : 'bot-message';

    // Convert markdown-style bold to HTML
    let formattedText = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

    messageDiv.innerHTML = `<div class="message-content">${formattedText}</div>`;
    messagesDiv.appendChild(messageDiv);
    scrollToBottom();
}

function scrollToBottom() {
    const messagesDiv = document.getElementById('chatMessages');
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

// Stock Watchlist
async function loadStockWatchlist() {
    try {
        const response = await fetch(`${API_BASE}/api/stocks`);
        const data = await response.json();

        const watchlistDiv = document.getElementById('stockWatchlist');
        watchlistDiv.innerHTML = '';

        data.stocks.forEach(stock => {
            const stockCard = document.createElement('div');
            stockCard.className = 'stock-card';
            stockCard.onclick = () => showStockDetail(stock.symbol);

            const changeClass = stock.change >= 0 ? 'positive' : 'negative';
            const changeSymbol = stock.change >= 0 ? '+' : '';

            stockCard.innerHTML = `
                <div class="symbol">${stock.symbol}</div>
                <div class="price">$${stock.price ? stock.price.toFixed(2) : 'N/A'}</div>
                <div class="change ${changeClass}">
                    ${changeSymbol}${stock.change.toFixed(2)} (${stock.change_percent.toFixed(2)}%)
                </div>
            `;

            watchlistDiv.appendChild(stockCard);
        });

    } catch (error) {
        console.error('Error loading watchlist:', error);
        document.getElementById('stockWatchlist').innerHTML = '<p>Error loading stocks</p>';
    }
}

// Stock Detail Modal
async function showStockDetail(symbol) {
    const modal = document.getElementById('stockModal');
    const detailDiv = document.getElementById('stockDetail');

    modal.style.display = 'block';
    detailDiv.innerHTML = '<div class="loading">Loading analysis...</div>';

    try {
        const response = await fetch(`${API_BASE}/api/stock/${symbol}`);
        const data = await response.json();

        if (data.error) {
            detailDiv.innerHTML = `<p>Error: ${data.error}</p>`;
            return;
        }

        const rec = data.recommendation;
        const tech = data.technical_analysis;

        let html = `
            <h2>${data.symbol} - $${data.current_price.toFixed(2)}</h2>
            <div class="stock-detail-grid">
                <div class="detail-section">
                    <h3>Recommendation</h3>
                    <div class="prediction-action ${rec.action}">${rec.action}</div>
                    <p><strong>Confidence:</strong> ${(rec.confidence * 100).toFixed(0)}%</p>
                    <p>${rec.rationale}</p>
                </div>

                <div class="detail-section">
                    <h3>Technical Indicators</h3>
                    <p><strong>RSI:</strong> ${tech.rsi ? tech.rsi.toFixed(2) : 'N/A'}</p>
                    <p><strong>Trend:</strong> ${tech.trend}</p>
                    <p><strong>Signal:</strong> ${tech.signals.signal.toUpperCase()}</p>
                </div>

                <div class="detail-section">
                    <h3>Sentiment Analysis</h3>
                    <p><strong>Sentiment:</strong> ${data.sentiment_analysis.sentiment}</p>
                    <p><strong>News Articles:</strong> ${data.news_count}</p>
                    <p>${data.sentiment_analysis.recommendation}</p>
                </div>
            </div>
            <p class="disclaimer">${rec.disclaimer}</p>
        `;

        detailDiv.innerHTML = html;

    } catch (error) {
        console.error('Error loading stock detail:', error);
        detailDiv.innerHTML = '<p>Error loading stock analysis</p>';
    }
}

function closeModal() {
    document.getElementById('stockModal').style.display = 'none';
}

// Click outside modal to close
window.onclick = function(event) {
    const modal = document.getElementById('stockModal');
    if (event.target == modal) {
        modal.style.display = 'none';
    }
}

// Chart
let stockChart = null;

async function loadChart() {
    const symbol = document.getElementById('chartSymbol').value;
    const period = document.getElementById('chartPeriod').value;

    try {
        const response = await fetch(`${API_BASE}/api/stock/${symbol}?period=${period}`);
        const data = await response.json();

        if (data.error || !data.historical_data) {
            console.error('Error loading chart data');
            return;
        }

        const chartData = data.historical_data;

        // Destroy previous chart if exists
        if (stockChart) {
            stockChart.destroy();
        }

        const ctx = document.getElementById('stockChart').getContext('2d');
        stockChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: chartData.dates,
                datasets: [{
                    label: `${symbol} Close Price`,
                    data: chartData.close,
                    borderColor: 'rgb(37, 99, 235)',
                    backgroundColor: 'rgba(37, 99, 235, 0.1)',
                    tension: 0.1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                plugins: {
                    legend: {
                        display: true,
                        position: 'top'
                    },
                    tooltip: {
                        mode: 'index',
                        intersect: false
                    }
                },
                scales: {
                    y: {
                        beginAtZero: false,
                        ticks: {
                            callback: function(value) {
                                return '$' + value.toFixed(2);
                            }
                        }
                    }
                }
            }
        });

    } catch (error) {
        console.error('Error loading chart:', error);
    }
}

// Predictions
async function loadPredictions() {
    try {
        const response = await fetch(`${API_BASE}/api/predictions?limit=20`);
        const data = await response.json();

        const listDiv = document.getElementById('predictionsList');

        if (data.predictions.length === 0) {
            listDiv.innerHTML = '<p>No predictions yet. Start analyzing stocks!</p>';
            return;
        }

        listDiv.innerHTML = '';

        data.predictions.forEach(pred => {
            const predDiv = document.createElement('div');
            predDiv.className = 'prediction-item';

            predDiv.innerHTML = `
                <div class="prediction-header">
                    <div>
                        <strong>${pred.symbol}</strong> -
                        <span class="prediction-action ${pred.prediction}">${pred.prediction}</span>
                    </div>
                    <div>${new Date(pred.timestamp).toLocaleDateString()}</div>
                </div>
                <p><strong>Confidence:</strong> ${(parseFloat(pred.confidence) * 100).toFixed(0)}%</p>
                <p><strong>Price:</strong> $${parseFloat(pred.price_at_prediction).toFixed(2)}</p>
                <p>${pred.rationale}</p>
                <p><strong>Outcome:</strong> ${pred.outcome}</p>
            `;

            listDiv.appendChild(predDiv);
        });

    } catch (error) {
        console.error('Error loading predictions:', error);
        document.getElementById('predictionsList').innerHTML = '<p>Error loading predictions</p>';
    }
}

// News
async function loadNews() {
    const symbol = document.getElementById('newsSymbol').value;
    const listDiv = document.getElementById('newsList');

    listDiv.innerHTML = '<div class="loading">Loading news...</div>';

    try {
        const url = symbol ? `${API_BASE}/api/news/${symbol}` : `${API_BASE}/api/news/AAPL`;
        const response = await fetch(url);
        const data = await response.json();

        listDiv.innerHTML = '';

        if (data.articles.length === 0) {
            listDiv.innerHTML = '<p>No news articles available.</p>';
            return;
        }

        // Show sentiment summary
        if (data.sentiment) {
            const sentimentDiv = document.createElement('div');
            sentimentDiv.className = 'news-item';
            sentimentDiv.style.background = '#e0f2fe';
            sentimentDiv.innerHTML = `
                <h3>📊 Sentiment Analysis</h3>
                <p><strong>Overall Sentiment:</strong> ${data.sentiment.sentiment.toUpperCase()}</p>
                <p><strong>Articles Analyzed:</strong> ${data.sentiment.article_count}</p>
                <p>${data.sentiment.recommendation}</p>
            `;
            listDiv.appendChild(sentimentDiv);
        }

        data.articles.forEach(article => {
            const newsDiv = document.createElement('div');
            newsDiv.className = 'news-item';

            newsDiv.innerHTML = `
                <h3>${article.title}</h3>
                <div class="news-meta">
                    ${article.source} • ${article.published_at ? new Date(article.published_at).toLocaleString() : ''}
                </div>
                <p>${article.description || article.snippet || ''}</p>
                <a href="${article.url}" target="_blank">Read more →</a>
            `;

            listDiv.appendChild(newsDiv);
        });

    } catch (error) {
        console.error('Error loading news:', error);
        listDiv.innerHTML = '<p>Error loading news</p>';
    }
}

// Accuracy Stats
async function loadAccuracy() {
    try {
        const response = await fetch(`${API_BASE}/api/accuracy`);
        const data = await response.json();

        const statsDiv = document.getElementById('accuracyStats');

        statsDiv.innerHTML = `
            <div class="accuracy-grid">
                <div class="accuracy-card">
                    <div class="stat-value">${data.total_predictions}</div>
                    <div class="stat-label">Total Predictions</div>
                </div>
                <div class="accuracy-card">
                    <div class="stat-value">${data.accuracy_percentage}%</div>
                    <div class="stat-label">Accuracy</div>
                </div>
                <div class="accuracy-card">
                    <div class="stat-value">${data.correct}</div>
                    <div class="stat-label">Correct</div>
                </div>
                <div class="accuracy-card">
                    <div class="stat-value">${data.incorrect}</div>
                    <div class="stat-label">Incorrect</div>
                </div>
                <div class="accuracy-card">
                    <div class="stat-value">${data.pending}</div>
                    <div class="stat-label">Pending</div>
                </div>
            </div>
        `;

    } catch (error) {
        console.error('Error loading accuracy:', error);
        document.getElementById('accuracyStats').innerHTML = '<p>Error loading accuracy stats</p>';
    }
}

// Tab Navigation
function openTab(event, tabName) {
    // Hide all tab contents
    const tabContents = document.getElementsByClassName('tab-content');
    for (let i = 0; i < tabContents.length; i++) {
        tabContents[i].classList.remove('active');
    }

    // Remove active class from all buttons
    const tabButtons = document.getElementsByClassName('tab-button');
    for (let i = 0; i < tabButtons.length; i++) {
        tabButtons[i].classList.remove('active');
    }

    // Show current tab and mark button as active
    document.getElementById(tabName).classList.add('active');
    event.currentTarget.classList.add('active');

    // Load data for specific tabs
    if (tabName === 'news') {
        loadNews();
    } else if (tabName === 'predictions') {
        loadPredictions();
    } else if (tabName === 'accuracy') {
        loadAccuracy();
    }
}

// Refresh data periodically
setInterval(() => {
    loadStockWatchlist();
}, 60000);  // Every minute
