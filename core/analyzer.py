import numpy as np

class ActivityAnalyzer:
    """
    Advanced cycling activity analyzer.
    Calculates NP, VI, Time in Zones, Matches Burned, Aerobic Decoupling, and Cadence Drop-off.
    """
    def __init__(self, power_stream: list[int], hr_stream: list[int], cadence_stream: list[int], ftp: int):
        # Replace None with 0 for calculations, and convert to numpy arrays for fast math
        self.power = np.array([p if p is not None else 0 for p in power_stream], dtype=float)
        self.hr = np.array([h if h is not None else 0 for h in hr_stream], dtype=float)
        self.cadence = np.array([c if c is not None else 0 for c in cadence_stream], dtype=float)
        self.ftp = ftp
        self.length = len(self.power)

    def calculate_normalized_power(self, power_array: np.ndarray) -> float:
        """Calculate Normalized Power (NP) using the standard 30-second moving average method."""
        if len(power_array) < 30:
            return float(np.mean(power_array)) if len(power_array) > 0 else 0.0
            
        # 30-second rolling average
        window = 30
        rolling_avg = np.convolve(power_array, np.ones(window)/window, mode='valid')
        # Raise to 4th power, take average, then 4th root
        np_val = np.mean(rolling_avg ** 4) ** 0.25
        return round(float(np_val), 1)

    def get_vi(self) -> float:
        """Variability Index: NP / Average Power"""
        avg_power = np.mean(self.power)
        if avg_power <= 0:
            return 1.0
        np_val = self.calculate_normalized_power(self.power)
        return round(np_val / avg_power, 2)

    def get_time_in_zones(self) -> dict[str, float]:
        """
        Calculate percentage of time spent in standard Coggan Power Zones.
        Returns percentages (0.0 to 100.0).
        """
        zones = {
            "Z1 (Recovery) <55%": self.power < (0.55 * self.ftp),
            "Z2 (Endurance) 55-75%": (self.power >= (0.55 * self.ftp)) & (self.power < (0.76 * self.ftp)),
            "Z3 (Tempo) 76-87%": (self.power >= (0.76 * self.ftp)) & (self.power < (0.88 * self.ftp)),
            "Z4 (Threshold) 88-105%": (self.power >= (0.88 * self.ftp)) & (self.power < (1.06 * self.ftp)),
            "Z5 (VO2Max) 106-120%": (self.power >= (1.06 * self.ftp)) & (self.power < (1.21 * self.ftp)),
            "Z6 (Anaerobic) 121-150%": (self.power >= (1.21 * self.ftp)) & (self.power < (1.51 * self.ftp)),
            "Z7 (Neuromuscular) >150%": self.power >= (1.51 * self.ftp),
        }
        
        tiz = {}
        if self.length == 0:
            return tiz
            
        for name, condition in zones.items():
            tiz[name] = round(float(np.sum(condition) / self.length * 100), 1)
        return tiz

    def get_matches_burned(self, threshold_pct=1.2, duration_sec=15) -> int:
        """
        Count 'matches burned'. A match is a surge above a certain threshold (e.g. 120% FTP)
        lasting for at least a certain duration (e.g. 15 seconds).
        """
        surge_threshold = self.ftp * threshold_pct
        is_surge = self.power > surge_threshold
        
        matches = 0
        current_surge_duration = 0
        
        for surging in is_surge:
            if surging:
                current_surge_duration += 1
            else:
                if current_surge_duration >= duration_sec:
                    matches += 1
                current_surge_duration = 0
                
        # Check if it ended during a surge
        if current_surge_duration >= duration_sec:
            matches += 1
            
        return matches

    def _estimate_warmup_duration(self, max_search_min=30) -> int:
        """
        Estimate warmup duration by finding the first time the 60-second moving average 
        exceeds 65% of FTP.
        Returns duration in seconds.
        """
        if self.length < 60:
            return 0
            
        threshold = self.ftp * 0.65
        window = 60
        rolling_avg = np.convolve(self.power, np.ones(window)/window, mode='valid')
        
        max_search_sec = min(max_search_min * 60, len(rolling_avg))
        
        for i in range(max_search_sec):
            if rolling_avg[i] >= threshold:
                return i + window
                
        return max_search_sec

    def get_aerobic_decoupling(self, exclude_edges=True) -> float | None:
        """
        Calculate Aerobic Decoupling (Pw:HR) by comparing the Efficiency Factor (EF = NP/AvgHR)
        of the first half vs the second half of the ride.
        If exclude_edges is True, automatically removes warm-up (dynamic or 15 min max) and cool-down (last 5 min).
        Returns percentage drift (e.g. 5.5 means 5.5% decoupling). Returns None if missing HR.
        """
        if self.length < 600 or np.mean(self.hr) < 50:
            # Need at least 10 minutes and valid HR data
            return None
            
        p_stream = self.power
        h_stream = self.hr
        
        # Smart trimming for Warm-up and Cool-down
        if exclude_edges:
            estimated_wu = self._estimate_warmup_duration()
            # Use the longer of estimated warmup or 15 minutes (900 seconds)
            wu_cut = max(estimated_wu, 900)
            cd_cut = 300 # 5 mins
            
            # Sanity check: If ride is too short to cut that much, fallback to proportional
            if wu_cut + cd_cut >= self.length * 0.7:
                wu_cut = int(self.length * 0.15)
                cd_cut = int(self.length * 0.10)
                
            p_stream = p_stream[wu_cut:self.length - cd_cut]
            h_stream = h_stream[wu_cut:self.length - cd_cut]
            
        stream_len = len(p_stream)
        if stream_len < 300: # Less than 5 mins left after trimming
            return None
            
        half = stream_len // 2
        p1, p2 = p_stream[:half], p_stream[half:]
        h1, h2 = h_stream[:half], h_stream[half:]
        
        avg_hr1, avg_hr2 = np.mean(h1), np.mean(h2)
        if avg_hr1 == 0 or avg_hr2 == 0:
            return None
            
        np1 = self.calculate_normalized_power(p1)
        np2 = self.calculate_normalized_power(p2)
        
        ef1 = np1 / avg_hr1
        ef2 = np2 / avg_hr2
        
        if ef1 == 0:
            return 0.0
            
        # Decoupling is typically calculated as (EF1 - EF2) / EF1 
        # (Since EF drops when HR rises for the same power)
        decoupling = ((ef1 - ef2) / ef1) * 100.0
        return round(float(decoupling), 2)

    def get_cadence_dropoff(self) -> float | None:
        """
        Calculate cadence drop-off between the first and second half of the ride.
        Returns the difference in RPM (negative means cadence dropped).
        """
        if self.length < 600 or np.mean(self.cadence) < 20:
            return None
            
        # Filter out 0 cadence (coasting) for more accurate pedaling averages
        half = self.length // 2
        c1 = self.cadence[:half]
        c2 = self.cadence[half:]
        
        c1_pedaling = c1[c1 > 0]
        c2_pedaling = c2[c2 > 0]
        
        if len(c1_pedaling) == 0 or len(c2_pedaling) == 0:
            return None
            
        avg_c1 = np.mean(c1_pedaling)
        avg_c2 = np.mean(c2_pedaling)
        
        dropoff = avg_c2 - avg_c1
        return round(float(dropoff), 1)

    def get_wbal_metrics(self, w_prime: int = 20000) -> dict:
        """
        Calculate W' balance (anaerobic battery) and return the max drop.
        Uses the Skiba (2012) differential model.
        """
        if self.length == 0 or self.ftp <= 0:
            return {"wbal_min": float(w_prime), "wbal_drop": 0.0}

        w_bal = float(w_prime)
        w_bal_history = []
        cp = float(self.ftp)

        for p in self.power:
            if p > cp:
                # Expenditure: Power above Critical Power (FTP)
                w_bal -= (p - cp)
            else:
                # Recovery: Power below CP
                d_cp = cp - p
                tau_w = 546 * np.exp(-0.01 * d_cp) + 316
                w_bal = w_prime - (w_prime - w_bal) * np.exp(-1 / tau_w)
            
            w_bal_history.append(w_bal)

        min_wbal = min(w_bal_history)
        wbal_drop = w_prime - min_wbal
        
        return {
            "wbal_min": round(float(min_wbal), 0),
            "wbal_drop": round(float(wbal_drop), 0)
        }

    def get_profile_fingerprint(self, workout_type_id: int | None = None) -> str:
        """
        Generate a unique profile key for finding similar activities in NoSQL.
        Format: {Format}_{Duration}_{Pacing}_{DominantZone}
        e.g., FreeRide_Med_Steady_Z2
        """
        if self.length == 0:
            return "Unknown_Short_Unknown_Z0"
            
        # 1. Format
        fmt = "FreeRide"
        if workout_type_id == 10:
            fmt = "Workout"
        elif workout_type_id == 11:
            fmt = "Race"
            
        # 2. Duration
        dur_mins = self.length / 60.0
        if dur_mins < 60:
            dur = "Short"
        elif dur_mins <= 120:
            dur = "Med"
        else:
            dur = "Long"
            
        # 3. Pacing
        vi = self.get_vi()
        if vi < 1.05:
            pac = "Steady"
        elif vi < 1.15:
            pac = "Variable"
        else:
            pac = "Punchy"
            
        # 4. Zone
        tiz = self.get_time_in_zones()
        dominant_zone = "Z1"
        max_pct = 0
        for z_full, pct in tiz.items():
            if pct > max_pct:
                max_pct = pct
                dominant_zone = z_full.split(" ")[0] # Extracts "Z1" from "Z1 (Recovery) <55%"
                
        return f"{fmt}_{dur}_{pac}_{dominant_zone}"

    def analyze_all(self, workout_type_id: int | None = None) -> dict:
        """Run all analyses and return a summary dictionary."""
        np_val = self.calculate_normalized_power(self.power)
        avg_power = np.mean(self.power) if self.length > 0 else 0
        
        # Precise Work (kJ) = Sum of Watts / 1000
        total_work_kj = np.sum(self.power) / 1000.0
        
        # Precise Averages (Filtered for active movement/pedaling)
        moving_hr = self.hr[self.power > 10] # Filter for power > 10W to exclude rests
        avg_hr_moving = np.mean(moving_hr) if len(moving_hr) > 0 else np.mean(self.hr)
        
        pedaling_cadence = self.cadence[self.cadence > 0]
        avg_cadence_pedaling = np.mean(pedaling_cadence) if len(pedaling_cadence) > 0 else 0

        wbal = self.get_wbal_metrics()
        
        return {
            "average_power": round(float(avg_power), 1),
            "normalized_power": np_val,
            "average_heartrate_active": round(float(avg_hr_moving), 1),
            "average_cadence_pedaling": round(float(avg_cadence_pedaling), 1),
            "total_work_kj": round(float(total_work_kj), 1),
            "variability_index": self.get_vi(),
            "aerobic_decoupling_pct": self.get_aerobic_decoupling(),
            "cadence_dropoff_rpm": self.get_cadence_dropoff(),
            "matches_burned": self.get_matches_burned(),
            "wbal_drop_kj": round(wbal["wbal_drop"] / 1000, 1), # In kJ for easier reading
            "time_in_zones": self.get_time_in_zones(),
            "profile_key": self.get_profile_fingerprint(workout_type_id)
        }
