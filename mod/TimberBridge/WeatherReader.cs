using Timberborn.GameCycleSystem;
using Timberborn.HazardousWeatherSystem;
using Timberborn.TimeSystem;
using Timberborn.WeatherSystem;

namespace TimberBridge {

  // Computes the weather forecast for /state. All services below are bound as
  // singletons in the "Game" context (verified by decompiling Timberborn
  // 1.0.13.1): WeatherService + HazardousWeatherService are ILoadableSingletons;
  // GameCycleService + IDayNightCycle are already used by StateReader.
  //
  // A cycle = TemperateWeatherDuration temperate days then HazardousWeatherDuration
  // hazard days. CycleLengthInDays = temperate + hazard. The hazard TYPE and
  // DURATION for the CURRENT cycle are already drawn and persisted, so the forecast
  // is exact while temperate. Once inside a hazard, the NEXT cycle's temperate
  // length is not yet drawn, so we return duration_days = -1 (unknown).
  public class WeatherReader {

    private readonly WeatherService _weather;
    private readonly HazardousWeatherService _hazard;
    private readonly GameCycleService _cycle;
    private readonly IDayNightCycle _time;

    public WeatherReader(WeatherService weather,
                         HazardousWeatherService hazard,
                         GameCycleService cycle,
                         IDayNightCycle time) {
      _weather = weather;
      _hazard = hazard;
      _cycle = cycle;
      _time = time;
    }

    public WeatherDto Read() {
      // Days elapsed into the current cycle. CycleDay is 1-based and only ticks on
      // DaytimeStartEvent; DayProgress (0..1) is the intra-day fraction.
      float intoCycle = (_cycle.CycleDay - 1) + _time.DayProgress;
      int temperate = _weather.TemperateWeatherDuration;
      int hazardLen = _weather.HazardousWeatherDuration;

      var dto = new WeatherDto();
      if (!_weather.IsHazardousWeather) {
        dto.current = "temperate";
        dto.current_ends_in_days = Max0(temperate - intoCycle);
        dto.next = new NextWeatherDto {
          type = HazardType(_hazard.CurrentCycleHazardousWeather),
          in_days = dto.current_ends_in_days,   // hazard begins when temperate ends
          duration_days = hazardLen
        };
      } else {
        dto.current = HazardType(_hazard.CurrentCycleHazardousWeather);
        int cycleLen = _weather.CycleLengthInDays;
        dto.current_ends_in_days = Max0(cycleLen - intoCycle);
        dto.next = new NextWeatherDto {
          type = "temperate",
          in_days = dto.current_ends_in_days,
          duration_days = -1                    // next cycle's temperate not yet drawn
        };
      }
      return dto;
    }

    private static float Max0(float v) { return v < 0f ? 0f : v; }

    private static string HazardType(IHazardousWeather hazard) {
      if (hazard is DroughtWeather) return "drought";
      if (hazard is BadtideWeather) return "badtide";
      return hazard != null ? hazard.Id : "unknown";
    }

    // DTOs serialized by Newtonsoft as part of the /state payload.
    public class WeatherDto {
      public string current;
      public float current_ends_in_days;
      public NextWeatherDto next;
    }

    public class NextWeatherDto {
      public string type;
      public float in_days;
      public int duration_days;
    }

  }

}
