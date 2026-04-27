"""Real-time AMS-color → filament-profile mapping (item #58).

Maps an RGB(A) tray color (8-char hex from the X2D's pushall state)
to the closest curated Bambu filament profile by Euclidean distance
in RGB space. Filters by material (PLA Basic / PLA Silk / PETG-HF /
…) when known. Source data: BambuStudio's filaments_color_codes.json
(7000+ entries covering Bambu's full BBL filament line).
"""
