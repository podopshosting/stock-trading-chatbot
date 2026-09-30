"""
Ultra-Lightweight ML Trading Agent for Lambda (Pure Python - No Dependencies)
Implements machine learning trading strategies without external libraries
"""
from typing import Dict, Optional, List
import math


class MLTradingAgent:
    """
    Pure Python ML trading agent - no numpy/sklearn required
    Uses ensemble of technical indicators and rule-based learning
    """

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()

    @staticmethod
    def mean(values: List[float]) -> float:
        """Calculate mean of list"""
        return sum(values) / len(values) if values else 0

    @staticmethod
    def std_dev(values: List[float]) -> float:
        """Calculate standard deviation"""
        if len(values) < 2:
            return 0
        avg = sum(values) / len(values)
        variance = sum((x - avg) ** 2 for x in values) / len(values)
        return math.sqrt(variance)

    def calculate_sma(self, prices: List[float], period: int) -> Optional[float]:
        """Simple Moving Average"""
        if len(prices) < period:
            return None
        return self.mean(prices[-period:])

    def calculate_ema(self, prices: List[float], period: int) -> Optional[float]:
        """Exponential Moving Average"""
        if len(prices) < period:
            return None

        multiplier = 2 / (period + 1)
        ema = self.mean(prices[:period])

        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema

        return ema

    def calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Relative Strength Index"""
        if len(prices) < period + 1:
            return None

        gains = []
        losses = []

        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        if len(gains) < period:
            return None

        avg_gain = self.mean(gains[-period:])
        avg_loss = self.mean(losses[-period:])

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def calculate_ema_series(self, prices: List[float],
                             period: int) -> List[float]:
        """Full EMA series, seeded with the SMA of the first `period` values.

        The MACD signal line is an EMA *of the MACD line*, so the MACD
        line has to exist as a series, not just as its latest value.
        """
        if len(prices) < period:
            return []

        multiplier = 2 / (period + 1)
        ema = self.mean(prices[:period])
        out = [ema]

        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
            out.append(ema)

        return out

    def calculate_macd(self, prices: List[float], fast: int = 12,
                       slow: int = 26,
                       signal_period: int = 9) -> Optional[Dict]:
        """MACD with a real signal line.

        This previously set `signal = macd * 0.9`, which is not a signal
        line at all. The consequences were not cosmetic:

          histogram = macd - 0.9*macd = 0.1*macd

        so the histogram always carried the sign of the MACD line, and
        the crossover test `macd > signal` reduced to `macd > 0` - i.e.
        to `EMA12 > EMA26`, a plain trend comparison. MACD's entire
        purpose is the crossover, and it carried none of that
        information. Measured over random walks it never once disagreed
        with a sign test on the MACD line.

        The signal line is now the `signal_period` EMA of the MACD line,
        which needs `slow + signal_period - 1` closes. Below that this
        returns None rather than inventing a number.
        """
        fast_series = self.calculate_ema_series(prices, fast)
        slow_series = self.calculate_ema_series(prices, slow)
        if not fast_series or not slow_series:
            return None

        # The two series start at different points in `prices` - the fast
        # one begins earlier - so they must be aligned before subtracting.
        offset = slow - fast
        macd_line = [fast_series[i + offset] - slow_series[i]
                     for i in range(len(slow_series))]

        signal_series = self.calculate_ema_series(macd_line, signal_period)
        if not signal_series:
            return None

        macd = macd_line[-1]
        signal = signal_series[-1]

        return {
            'macd': macd,
            'signal': signal,
            'histogram': macd - signal,
        }

    def calculate_bollinger_bands(self, prices: List[float], period: int = 20) -> Optional[Dict]:
        """Bollinger Bands"""
        if len(prices) < period:
            return None

        sma = self.calculate_sma(prices, period)
        std = self.std_dev(prices[-period:])

        return {
            'upper': sma + (2 * std),
            'middle': sma,
            'lower': sma - (2 * std)
        }

    # How many independent opinions the model can have at most. Used to
    # scale confidence, so agreement is measured against what was
    # available rather than against however many signals happened to fire.
    CORRELATION_GROUPS = ('trend', 'momentum', 'mean_reversion')

    def _aggregate_signals(self, signals):
        """Combine signals, counting each correlation group once.

        The previous aggregation counted every firing signal as an
        independent vote and averaged their confidences. Because three of
        the six measured the same thing, a single trend observation could
        be counted three times and reported as agreement - which is how
        AAPL came to display "72.5% confidence" derived from one fact
        seen twice.

        Within a group, votes are netted: signals that disagree cancel
        rather than both counting. Across groups they vote as independent
        opinions, and confidence scales with how many groups agree out of
        how many held an opinion at all.

        Returns (recommendation, confidence, group_detail, tally).
        """
        direction_value = {'buy': 1, 'sell': -1, 'hold': 0}
        groups = {}

        for name, group, direction, conf in signals:
            bucket = groups.setdefault(group, {
                'signals': [], 'net': 0.0, 'max_confidence': 0.0,
            })
            bucket['signals'].append(
                {'name': name, 'direction': direction,
                 'confidence': round(conf, 4)}
            )
            bucket['net'] += direction_value[direction] * conf
            if direction != 'hold':
                bucket['max_confidence'] = max(bucket['max_confidence'], conf)

        group_detail = {}
        group_votes = []

        for group, bucket in groups.items():
            net = bucket['net']
            if net > 1e-9:
                group_direction = 'buy'
            elif net < -1e-9:
                group_direction = 'sell'
            else:
                group_direction = 'hold'

            contributing = [s for s in bucket['signals']
                            if s['direction'] != 'hold']
            directions = {s['direction'] for s in contributing}
            # Internal disagreement is real information: it means the
            # group's own measurements conflict, so its opinion is weaker.
            internal_agreement = 1.0 if len(directions) <= 1 else 0.5

            group_confidence = bucket['max_confidence'] * internal_agreement

            group_detail[group] = {
                'direction': group_direction,
                # One opinion per group, however many signals fired.
                'counted': 0 if group_direction == 'hold' else 1,
                'signals_fired': len(bucket['signals']),
                'signal_names': [s['name'] for s in bucket['signals']],
                'net': round(net, 4),
                'internal_agreement': internal_agreement,
                'confidence': round(group_confidence, 4),
            }

            if group_direction != 'hold':
                group_votes.append((group_direction, group_confidence))

        buy_groups = [c for d, c in group_votes if d == 'buy']
        sell_groups = [c for d, c in group_votes if d == 'sell']

        # Backward-compatible tally. `total` now counts the independent
        # opinions, not the raw signals, which is what the confidence is
        # actually based on.
        tally = {
            'buy': len(buy_groups),
            'sell': len(sell_groups),
            'hold': sum(1 for g in group_detail.values()
                        if g['direction'] == 'hold'),
            'total': len(group_votes),
            'signals_fired': len(signals),
            'groups': group_detail,
        }

        if not group_votes:
            return 'hold', 0.5, group_detail, tally

        if len(buy_groups) > len(sell_groups):
            recommendation, winning = 'buy', buy_groups
        elif len(sell_groups) > len(buy_groups):
            recommendation, winning = 'sell', sell_groups
        else:
            # Groups split evenly: that is genuine indecision, not a
            # coin toss to be dressed up as a call.
            return 'hold', 0.5, group_detail, tally

        # Confidence is the winning groups' strength, scaled by the share
        # of opinionated groups that agreed. Unanimity across three
        # groups can approach the strongest signal's confidence; a 2-1
        # split cannot.
        agreement_share = len(winning) / len(group_votes)
        confidence = self.mean(winning) * agreement_share

        return recommendation, max(0.0, min(1.0, confidence)), group_detail, tally

    def analyze_stock(self, prices: List[float], volume: List[int] = None) -> Dict:
        """
        Comprehensive ML-based stock analysis

        Args:
            prices: List of closing prices (oldest to newest)
            volume: Optional list of volume data

        Returns:
            Complete analysis with recommendation
        """
        if len(prices) < 50:
            return {
                'error': 'Need at least 50 days of price data',
                'recommendation': 'hold',
                'confidence': 0.0
            }

        current_price = prices[-1]

        # Calculate all indicators
        sma20 = self.calculate_sma(prices, 20)
        sma50 = self.calculate_sma(prices, 50)
        sma200 = self.calculate_sma(prices, 200) if len(prices) >= 200 else None
        rsi = self.calculate_rsi(prices, 14)
        macd_data = self.calculate_macd(prices)
        bb = self.calculate_bollinger_bands(prices, 20)

        # Calculate momentum
        momentum_10 = ((prices[-1] - prices[-10]) / prices[-10] * 100) if len(prices) >= 10 else 0
        momentum_20 = ((prices[-1] - prices[-20]) / prices[-20] * 100) if len(prices) >= 20 else 0

        # Volatility
        volatility = self.std_dev(prices[-20:])
        volatility_pct = (volatility / current_price) * 100

        # Generate signals, each tagged with the correlation group it
        # belongs to. Signals in the same group measure substantially the
        # same thing, so they must contribute ONE opinion between them.
        #
        # Groups were chosen from measured pairwise correlation of the
        # directional votes over 900 random-walk series, not by intuition:
        #
        #   rsi      vs momentum   r = -0.571   same quantity, opposite reading
        #   macd     vs momentum   r = +0.491
        #   macd     vs rsi        r = -0.466
        #   ma_cross vs golden     r = +0.237
        #   ma_cross vs macd       r = -0.058   (was near-duplicate before
        #                                        the MACD signal-line fix)
        signals = []      # (name, group, direction, confidence)

        # RSI - momentum oscillator
        if rsi is not None:
            if rsi < 30:
                signals.append(('rsi', 'momentum', 'buy', 0.85))
            elif rsi > 70:
                signals.append(('rsi', 'momentum', 'sell', 0.85))
            elif 40 <= rsi <= 60:
                signals.append(('rsi', 'momentum', 'hold', 0.60))

        # Moving average crossover - trend
        if sma20 and sma50:
            if sma20 > sma50 * 1.02:
                signals.append(('ma_crossover', 'trend', 'buy', 0.75))
            elif sma20 < sma50 * 0.98:
                signals.append(('ma_crossover', 'trend', 'sell', 0.75))

        # Golden / death cross - trend
        if sma50 and sma200:
            if sma50 > sma200:
                signals.append(('golden_cross', 'trend', 'buy', 0.70))
            else:
                signals.append(('golden_cross', 'trend', 'sell', 0.70))

        # MACD crossover - momentum
        if macd_data:
            if macd_data['macd'] > macd_data['signal']:
                signals.append(('macd', 'momentum', 'buy', 0.70))
            else:
                signals.append(('macd', 'momentum', 'sell', 0.70))

        # Bollinger bands - mean reversion
        if bb:
            if current_price < bb['lower']:
                signals.append(('bollinger', 'mean_reversion', 'buy', 0.80))
            elif current_price > bb['upper']:
                signals.append(('bollinger', 'mean_reversion', 'sell', 0.80))

        # 10-day momentum - momentum
        if momentum_10 > 5:
            signals.append(('momentum_10d', 'momentum', 'buy', 0.65))
        elif momentum_10 < -5:
            signals.append(('momentum_10d', 'momentum', 'sell', 0.65))

        recommendation, confidence, group_detail, tally = \
            self._aggregate_signals(signals)

        # Generate detailed reasoning
        reasoning = self._generate_reasoning(
            rsi, sma20, sma50, macd_data, bb, current_price,
            momentum_10, volatility_pct
        )

        return {
            'symbol': self.symbol,
            'recommendation': recommendation.upper(),
            'confidence': round(confidence, 2),
            'ml_score': round(confidence * 100, 1),
            'signals': tally,
            'indicators': {
                'rsi': round(rsi, 2) if rsi else None,
                'sma_20': round(sma20, 2) if sma20 else None,
                'sma_50': round(sma50, 2) if sma50 else None,
                'macd': round(macd_data['macd'], 2) if macd_data else None,
                'macd_signal': (round(macd_data['signal'], 4)
                                if macd_data else None),
                'macd_histogram': (round(macd_data['histogram'], 4)
                                   if macd_data else None),
                'momentum_10d': round(momentum_10, 2),
                'volatility_pct': round(volatility_pct, 2)
            },
            'reasoning': reasoning,
            'risk_level': self._assess_risk(volatility_pct, rsi),
            'confidence_meaning': (
                'agreement among independent indicator groups and the '
                'strength of the signals that fired. This is not a '
                'probability that the price will move in this direction, '
                'and not an expected return.'
            ),
        }

    def _generate_reasoning(self, rsi, sma20, sma50, macd_data, bb, price, momentum, volatility):
        """Generate human-readable reasoning"""
        reasons = []

        if rsi:
            if rsi < 30:
                reasons.append(f"🔴 RSI oversold ({rsi:.1f})")
            elif rsi > 70:
                reasons.append(f"🔴 RSI overbought ({rsi:.1f})")
            else:
                reasons.append(f"✓ RSI neutral ({rsi:.1f})")

        if sma20 and sma50:
            if sma20 > sma50:
                reasons.append("✓ Bullish MA trend")
            else:
                reasons.append("✗ Bearish MA trend")

        if macd_data:
            if macd_data['histogram'] > 0:
                reasons.append("✓ MACD bullish")
            else:
                reasons.append("✗ MACD bearish")

        if bb and price:
            if price < bb['lower']:
                reasons.append("💡 Price below lower BB")
            elif price > bb['upper']:
                reasons.append("⚠️ Price above upper BB")

        if abs(momentum) > 5:
            direction = "up" if momentum > 0 else "down"
            reasons.append(f"📈 Strong momentum {direction}")

        return " | ".join(reasons)

    def _assess_risk(self, volatility_pct, rsi):
        """Assess risk level"""
        if volatility_pct > 5 or (rsi and (rsi < 25 or rsi > 75)):
            return "HIGH"
        elif volatility_pct > 3 or (rsi and (rsi < 35 or rsi > 65)):
            return "MEDIUM"
        else:
            return "LOW"


def get_ml_recommendation(symbol: str, prices: List[float]) -> Dict:
    """Quick ML recommendation"""
    agent = MLTradingAgent(symbol)
    return agent.analyze_stock(prices)
