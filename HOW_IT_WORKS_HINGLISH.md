# Platform ka actual workflow

1. YouTube playlist/video add karo. Playlist ke videos individually queue hote hain; duplicate records nahi bante. Processing page par progress, error aur retry milta hai.
2. Video ka audio timestamped transcript banta hai. Visual timeline bhi scan hoti hai, including silent trade actions. Har detected event ke aas-paas chart frames save hote hain.
3. AI original frames aur speech se trade candidates extract aur validate karta hai. Sirf position drawing ko filled trade nahi maana jaata. Price/outcome unclear ho to Unknown; R:R actual supported prices se calculate hota hai.
4. Knowledge Base mein spoken lessons, visual readings aur trade evidence source timestamps ke saath milte hain. Chat isi stored evidence se jawab deta hai. Teaching patterns aur actual execution patterns alag hain; unknown outcomes win rate mein count nahi hote.
5. Same setup do videos mein repeat ho to rule draft automatically banta hai. Jo parameters video mein nahi diye, wo invent nahi hote. Backtesting page par exact mechanical interpretation review karo. Current engines sweep/rejection aur range breakout hain; discretionary IFVG, session ya multi-timeframe filters automatically reproduce nahi hote.
6. Historical OHLC CSV import karo ya OANDA candles fetch karo. Reviewed rule ka backtest next-candle entry, spread, slippage, stop-first ambiguous bars aur video publication ke baad wale independent holdout par run hota hai.
7. Charts page fresh OANDA quotes par eligible tested rule check karta hai. Valid trigger par PAPER_SETUP entry/stop/target dikhta hai; missing/stale data, weak sample, changed evidence ya no trigger par WAIT. Broker order place nahi hota.

Run: .\.venv\Scripts\python.exe run.py; phir http://localhost:8000 kholo. GEMINI_API_KEY aur FFmpeg required hain. Live data ke liye .env mein OANDA_API_TOKEN, OANDA_ACCOUNT_ID aur OANDA_ENVIRONMENT configure karo. TradingView password app ko nahi chahiye.

100% accurate prediction, guaranteed profit ya har discretionary trade ka exact recreation possible nahi hai. AI evidence readings aur bar backtests ko source frames aur paper forward results ke saath review karo. Data SQLite aur evidence folders mein persist hota hai; dono ka backup lo.
