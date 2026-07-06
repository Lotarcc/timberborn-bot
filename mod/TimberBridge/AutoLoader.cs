using System;
using System.IO;
using Timberborn.GameSaveRepositorySystem;
using Timberborn.GameSceneLoading;
using Timberborn.MapRepositorySystem;
using Timberborn.SingletonSystem;
using UnityEngine;

namespace TimberBridge {

  // Dev/automation auto-entry: at the main menu, if a marker file is present,
  // enter a game one frame later (so the scene transition is safe) and delete
  // the marker (one-shot). Marker content selects the action:
  //   ""        or "recent"          -> load most recent save (new game if none)
  //   "new"                          -> new game, first builtin map, Folktails
  //   "new:<map>"                    -> new game on <map>
  //   "new:<map>:<faction>"          -> new game on <map> with <faction>
  // The reload script drops the marker before relaunching. All calls use the
  // game's own GameSceneLoader (in-process, reliable — no synthesized input).
  public class AutoLoader : ILoadableSingleton, IUpdatableSingleton {

    private const string MarkerFile = "timberbridge_autoload.flag";
    private const string DefaultFaction = "Folktails";
    private const string DefaultSettlementName = "LLM Colony";

    private readonly GameSceneLoader _sceneLoader;
    private readonly GameSaveRepository _saveRepo;
    private readonly MapRepository _mapRepo;
    private bool _armed;
    private string _command = "";

    public AutoLoader(GameSceneLoader sceneLoader,
                      GameSaveRepository saveRepo,
                      MapRepository mapRepo) {
      _sceneLoader = sceneLoader;
      _saveRepo = saveRepo;
      _mapRepo = mapRepo;
    }

    public void Load() {
      string marker = Path.Combine(Application.persistentDataPath, MarkerFile);
      if (File.Exists(marker)) {
        try { _command = (File.ReadAllText(marker) ?? "").Trim(); } catch { _command = ""; }
        try { File.Delete(marker); } catch { /* best effort */ }
        _armed = true;
        Debug.Log("[TimberBridge] autoload marker found; command='" + _command + "'");
      }
    }

    public void UpdateSingleton() {
      if (!_armed) {
        return;
      }
      _armed = false; // one-shot
      try {
        if (_command.StartsWith("new", StringComparison.OrdinalIgnoreCase)) {
          StartNewGame(_command);
        } else {
          SaveReference recent = _saveRepo.GetMostRecentSave();
          if (_saveRepo.SaveExists(recent)) {
            Debug.Log("[TimberBridge] loading most recent save.");
            _sceneLoader.StartSaveGameInstantly(recent);
          } else {
            Debug.Log("[TimberBridge] no save found; starting a new game.");
            StartNewGame("new");
          }
        }
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] autoload failed: " + e);
      }
    }

    private void StartNewGame(string command) {
      string mapName = null;
      string faction = DefaultFaction;
      string[] parts = command.Split(':');
      if (parts.Length >= 2 && parts[1].Length > 0) { mapName = parts[1]; }
      if (parts.Length >= 3 && parts[2].Length > 0) { faction = parts[2]; }
      if (mapName == null) {
        foreach (string n in _mapRepo.GetBuiltinMapNames()) { mapName = n; break; } // first builtin
      }
      MapFileReference mapRef = MapFileReference.FromResource(mapName);
      Debug.Log("[TimberBridge] starting new game: faction=" + faction + " map=" + mapName);
      _sceneLoader.StartNewGameInstantly(faction, mapRef, DefaultSettlementName);
    }

  }

}
