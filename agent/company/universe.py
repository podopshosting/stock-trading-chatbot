"""Seed candidate pool for peer discovery: TICKERS ONLY. Classification of
each ticker comes from the SEC (SIC code), never from this list, so the
list cannot assert that two companies are alike. A reviewable, static set
of large US-listed names; extend deliberately."""
SEED_UNIVERSE = [
    # packaged food / beverage / staples
    "GIS", "CPB", "KHC", "CAG", "SJM", "HRL", "MKC", "HSY", "MDLZ", "K",
    "KDP", "PEP", "KO", "TSN", "LW", "POST", "THS", "BGS", "FLO", "CL",
    "PG", "KMB", "CHD", "CLX", "WMT", "COST", "KR",
    # tech
    "AAPL", "MSFT", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "MU",
    "GOOGL", "META", "AMZN", "TSLA", "ORCL", "CRM", "ADBE", "CSCO", "IBM",
    # energy / financials / health / industrial
    "XOM", "CVX", "COP", "JPM", "BAC", "WFC", "GS", "MS", "JNJ", "PFE",
    "MRK", "LLY", "ABBV", "UNH", "CAT", "DE", "BA", "GE", "HON", "UPS",
    # utilities / REIT / telecom
    "NEE", "DUK", "SO", "T", "VZ", "TMUS", "O", "PLD",
]
