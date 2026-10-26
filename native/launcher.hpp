#pragma once
// Spatial Mode launcher (FW-15, Launcher A): the Applications grid opened from
// the button at the shelf's left end. A curved Palladium panel at eye level
// with a Search Applications field (typed on the Mac keyboard), Recent Apps,
// and All Applications as a paged grid with folders that open in place.
// Holding the trigger on an icon picks it up; releasing it between two icons
// moves it there. This file is the data, layout, hit testing and gestures,
// apart from drawing (launcher_render.hpp) and the tiles (spatial.hpp), so the
// self-tests can run it.
//
// The companion owns the data: launcher.json beside the spatial state holds the
// catalogue (apps, the arranged items with folders, recent apps), and the state's
// "launcher" object holds open/seq, the search query, its results and the
// keyboard selection. The host owns the page, the open folder and drags.
#include "host.hpp"
#include "protocol.hpp"
#include "shelf.hpp"
#include <map>

namespace fw {

// Layout in the approved mockup's units (its panel is 600 wide), so the
// geometry reads like evidence/launcher-mockup.html option A.
inline constexpr float launcherUnits = 600, launcherUnitsHigh = 372;
inline constexpr float launcherWidth = 1.1f;                       // metres
inline constexpr float launcherHeight = launcherWidth * launcherUnitsHigh / launcherUnits;
inline constexpr float launcherDistance = 1.15f;                   // from the head, at open
inline constexpr float launcherDrop = 0;                           // centre below eye level
inline constexpr float launcherDimDistance = 1.4f;                 // the dimming backdrop
inline constexpr int launcherColumns = 8, launcherRows = 3, launcherPerPage = launcherColumns * launcherRows;
inline constexpr int launcherRecentMax = 8;
inline constexpr float launcherPixelsPerUnit = 2.4f;               // texture: 1440 x 893
inline constexpr float launcherCell = 66, launcherRowStep = 60, launcherGridTop = 168, launcherIcon = 34;
inline constexpr float launcherRecentStep = 68, launcherRecentTop = 78, launcherRecentIcon = 30;
inline constexpr float launcherDotsY = 352, launcherDotStep = 13;
inline constexpr double launcherEdgeFlipMs = 650;                  // a held icon at the edge turns the page

// OpenVR curvature that wraps the panel round the head at its distance.
inline float launcherCurvature(float width = launcherWidth, float radius = launcherDistance) {
  return width / (2 * float(M_PI) * radius);
}

inline bool launcherBundleID(const std::string &id) {
  if (id.empty() || id.size() > 200 || !std::isalnum((unsigned char)id[0])) return false;
  return std::all_of(id.begin(), id.end(), [](unsigned char c) {
    return std::isalnum(c) || c == '.' || c == '_' || c == '-';
  });
}
inline bool launcherFolderID(const std::string &id) {
  return !id.empty() && id.size() <= 40 && std::all_of(id.begin(), id.end(), [](unsigned char c) {
    return std::isalnum(c) || c == '_' || c == '-';
  });
}

struct LauncherApp {
  std::string id, name;
};
// A grid item: an app (app >= 0) or a folder of apps.
struct LauncherItem {
  bool folder = false;
  int app = -1;
  std::string id, name; // folder id and name
  std::vector<int> apps;
  std::string key() const; // "app:<bundle>" or "folder:<id>", defined below
};

struct LauncherCatalog {
  int64_t catalog = -1;
  std::vector<LauncherApp> apps;
  std::map<std::string, int> index;
  std::vector<LauncherItem> items;
  std::vector<int> recent;
  int app(const std::string &id) const {
    auto it = index.find(id);
    return it == index.end() ? -1 : it->second;
  }
  // launcher.json, bounded and checked like the companion's writer does.
  static bool parse(const std::string &raw, LauncherCatalog &out) {
    auto j = json(raw);
    if (!j || !json_object_is_type(j.get(), json_type_object)) return false;
    LauncherCatalog c;
    auto number = field(j, "catalog");
    if (!number || !json_object_is_type(number, json_type_int)) return false;
    c.catalog = json_object_get_int64(number);
    auto apps = field(j, "apps"), items = field(j, "items"), recent = field(j, "recent");
    if (!apps || !items || !json_object_is_type(apps, json_type_array) || !json_object_is_type(items, json_type_array))
      return false;
    for (size_t i = 0; i < json_object_array_length(apps) && i < 600; ++i) {
      Json a(json_object_get(json_object_array_get_idx(apps, i)), json_object_put);
      auto id = str(a, "id");
      if (!launcherBundleID(id) || c.index.count(id)) continue;
      auto name = str(a, "name").substr(0, 128);
      c.index[id] = int(c.apps.size());
      c.apps.push_back({id, name.empty() ? id : name});
    }
    std::vector<bool> placed(c.apps.size());
    auto take = [&](const std::string &id) {
      const int k = c.app(id);
      if (k < 0 || placed[k]) return -1;
      placed[k] = true;
      return k;
    };
    for (size_t i = 0; i < json_object_array_length(items) && i < 600; ++i) {
      Json item(json_object_get(json_object_array_get_idx(items, i)), json_object_put);
      LauncherItem entry;
      if (auto members = field(item, "apps")) {
        entry.folder = true;
        entry.id = str(item, "folder");
        entry.name = str(item, "name").substr(0, 64);
        if (!launcherFolderID(entry.id) || !json_object_is_type(members, json_type_array)) continue;
        for (size_t k = 0; k < json_object_array_length(members) && k < 600; ++k) {
          auto m = json_object_array_get_idx(members, k);
          if (m && json_object_is_type(m, json_type_string)) {
            const int app = take(json_object_get_string(m));
            if (app >= 0) entry.apps.push_back(app);
          }
        }
        if (entry.apps.empty()) continue;
        if (entry.name.empty()) entry.name = "Untitled Folder";
      } else {
        entry.app = take(str(item, "app"));
        if (entry.app < 0) continue;
      }
      c.items.push_back(entry);
    }
    // Every app is reachable: anything the items left out joins at the end.
    for (size_t k = 0; k < c.apps.size(); ++k)
      if (!placed[k]) c.items.push_back({false, int(k), "", "", {}});
    if (recent && json_object_is_type(recent, json_type_array))
      for (size_t i = 0; i < json_object_array_length(recent) && int(c.recent.size()) < launcherRecentMax; ++i) {
        auto r = json_object_array_get_idx(recent, i);
        const int k = r && json_object_is_type(r, json_type_string) ? c.app(json_object_get_string(r)) : -1;
        if (k >= 0 && std::find(c.recent.begin(), c.recent.end(), k) == c.recent.end()) c.recent.push_back(k);
      }
    out = std::move(c);
    return true;
  }
};

struct LauncherState {
  bool available = false, open = false, keyboard = false;
  int64_t seq = 0, catalog = -1;
  std::string query;
  std::vector<std::string> results;
  int selected = 0, pageStep = 0;
  bool searching() const {
    return std::any_of(query.begin(), query.end(), [](unsigned char c) { return !std::isspace(c); });
  }
  static LauncherState parse(json_object *raw) {
    LauncherState s;
    if (!raw || !json_object_is_type(raw, json_type_object)) return s;
    Json j(json_object_get(raw), json_object_put);
    s.available = flag(j, "available");
    s.open = s.available && flag(j, "open");
    s.keyboard = flag(j, "keyboard");
    auto integer = [&](const char *k, int64_t fallback) {
      auto v = field(j, k);
      return v && json_object_is_type(v, json_type_int) ? json_object_get_int64(v) : fallback;
    };
    s.seq = std::max<int64_t>(0, integer("seq", 0));
    s.catalog = integer("catalog", -1);
    s.selected = int(std::clamp<int64_t>(integer("selected", 0), 0, 599));
    s.pageStep = int(std::clamp<int64_t>(integer("pageStep", 0), -1000000, 1000000));
    s.query = str(j, "query").substr(0, 256);
    auto results = field(j, "results");
    if (results && json_object_is_type(results, json_type_array))
      for (size_t i = 0; i < json_object_array_length(results) && i < 600; ++i) {
        auto r = json_object_array_get_idx(results, i);
        if (r && json_object_is_type(r, json_type_string) && launcherBundleID(json_object_get_string(r)))
          s.results.push_back(json_object_get_string(r));
      }
    return s;
  }
};

inline std::string LauncherItem::key() const {
  return folder ? "folder:" + id : "app:" + std::to_string(app);
}

// One grid cell's content: an app or a folder (by item index).
struct LauncherCell {
  int app = -1, item = -1;
  bool folder() const { return item >= 0 && app < 0; }
};

// What the grid shows now: the search results while searching, else the open
// folder, else the arranged items. `container` names what a drop reorders
// ("" the top level, else the folder id; none while searching).
struct LauncherListing {
  std::vector<LauncherCell> cells;
  bool searching = false, inFolder = false;
  std::string container, folderName;
  int pages() const { return std::max(1, int((cells.size() + launcherPerPage - 1) / launcherPerPage)); }
};
inline LauncherListing launcherListing(const LauncherCatalog &c, const LauncherState &s, const std::string &folder) {
  LauncherListing l;
  if (s.searching()) {
    l.searching = true;
    for (const auto &id : s.results) {
      const int k = c.app(id);
      if (k >= 0) l.cells.push_back({k, -1});
    }
    return l;
  }
  for (size_t i = 0; i < c.items.size(); ++i)
    if (c.items[i].folder && c.items[i].id == folder) {
      l.inFolder = true;
      l.container = folder;
      l.folderName = c.items[i].name;
      for (int app : c.items[i].apps) l.cells.push_back({app, int(i)});
      return l;
    }
  for (size_t i = 0; i < c.items.size(); ++i)
    l.cells.push_back({c.items[i].folder ? -1 : c.items[i].app, int(i)});
  return l;
}

// Identity used in requests and gesture keys: a bundle ID, or "folder:<id>".
inline std::string launcherCellID(const LauncherCatalog &c, const LauncherCell &cell) {
  if (cell.app >= 0) return c.apps[cell.app].id;
  return cell.item >= 0 ? "folder:" + c.items[cell.item].id : "";
}

// Panel units (x right from the left edge, y down from the top) of grid slot
// `slot` on a page: the icon's centre x and top.
inline void launcherSlot(int slot, float &cx, float &top) {
  const int col = slot % launcherColumns, row = slot / launcherColumns;
  cx = launcherUnits / 2 + (col - (launcherColumns - 1) / 2.f) * launcherCell;
  top = launcherGridTop + row * launcherRowStep;
}
inline float launcherRecentX(int i, int count) {
  return launcherUnits / 2 + (i - (count - 1) / 2.f) * launcherRecentStep;
}
inline float launcherDotX(int page, int pages) {
  return launcherUnits / 2 + (page - (pages - 1) / 2.f) * launcherDotStep;
}
// The Back button shown in an open folder, in panel units.
inline constexpr float launcherBackX = 26, launcherBackY = 141, launcherBackW = 58, launcherBackH = 20;

// What the laser is on, in panel units.
struct LauncherTarget {
  enum Kind { None, Panel, Field, Recent, Cell, Dot, Back } kind = None;
  int index = -1; // recent index, cell index in the listing, or page
};
inline LauncherTarget launcherHit(float x, float y, const LauncherListing &l, int recentCount, int page) {
  LauncherTarget t;
  if (x < 0 || y < 0 || x > launcherUnits || y > launcherUnitsHigh) return t;
  t.kind = LauncherTarget::Panel;
  if (std::fabs(x - launcherUnits / 2) <= 145 && y >= 14 && y <= 50) {
    t.kind = LauncherTarget::Field;
    return t;
  }
  if (l.inFolder && !l.searching && x >= launcherBackX && x <= launcherBackX + launcherBackW &&
      y >= launcherBackY - 3 && y <= launcherBackY + launcherBackH + 3) {
    t.kind = LauncherTarget::Back;
    return t;
  }
  if (recentCount > 0 && y >= launcherRecentTop - 6 && y < 132) {
    for (int i = 0; i < recentCount; ++i)
      if (std::fabs(x - launcherRecentX(i, recentCount)) <= launcherRecentStep / 2) {
        t.kind = LauncherTarget::Recent;
        t.index = i;
      }
    return t;
  }
  if (y >= launcherGridTop - 6 && y < launcherGridTop + launcherRows * launcherRowStep - 6) {
    const int row = int((y - (launcherGridTop - 6)) / launcherRowStep);
    const float first = launcherUnits / 2 - launcherColumns / 2.f * launcherCell;
    const int col = int(std::floor((x - first) / launcherCell));
    const int slot = row * launcherColumns + col;
    const int cell = page * launcherPerPage + slot;
    if (col >= 0 && col < launcherColumns && cell < int(l.cells.size())) {
      t.kind = LauncherTarget::Cell;
      t.index = cell;
    }
    return t;
  }
  const int pages = l.pages();
  if (pages > 1 && std::fabs(y - launcherDotsY) <= 12)
    for (int p = 0; p < pages; ++p)
      if (std::fabs(x - launcherDotX(p, pages)) <= launcherDotStep / 2) {
        t.kind = LauncherTarget::Dot;
        t.index = p;
      }
  return t;
}

// Gesture keys (SpatialPress binds a press to one): "launcher:recent:<bundle>",
// "launcher:cell:<bundle or folder:id>", "launcher:dot:<page>", "launcher:back",
// "launcher:field", "launcher:panel".
inline std::string launcherTargetKey(const LauncherCatalog &c, const LauncherListing &l, const LauncherTarget &t) {
  switch (t.kind) {
  case LauncherTarget::Recent: return "launcher:recent:" + c.apps[c.recent[t.index]].id;
  case LauncherTarget::Cell: return "launcher:cell:" + launcherCellID(c, l.cells[t.index]);
  case LauncherTarget::Dot: return "launcher:dot:" + std::to_string(t.index);
  case LauncherTarget::Back: return "launcher:back";
  case LauncherTarget::Field: return "launcher:field";
  case LauncherTarget::Panel: return "launcher:panel";
  default: return "";
  }
}

// Where a carried icon would land: the gap before cell `gap` (listing index,
// cells.size() for the end), or `onto` when it is over an icon's middle
// (dropping onto an app does nothing; no folders are made).
struct LauncherGap {
  int gap = -1;
  bool onto = false;
  explicit operator bool() const { return gap >= 0 && !onto; }
};
inline LauncherGap launcherGapAt(float x, float y, int page, int count) {
  LauncherGap g;
  if (y < launcherGridTop - 10 || y > launcherGridTop + launcherRows * launcherRowStep) return g;
  const float first = launcherUnits / 2 - launcherColumns / 2.f * launcherCell;
  const int row = std::clamp(int((y - (launcherGridTop - 6)) / launcherRowStep), 0, launcherRows - 1);
  const float u = (x - first) / launcherCell;
  if (u < -.5f || u > launcherColumns + .5f) return g;
  const int col = std::clamp(int(std::floor(u)), 0, launcherColumns - 1);
  const float within = u - col; // 0..1 across the cell
  const int base = page * launcherPerPage + row * launcherColumns;
  const int cell = base + col;
  if (cell >= count) {
    // Past the last icon: the end of the list (when on its page).
    if (page * launcherPerPage <= count) g.gap = count;
    return g;
  }
  if (within > .3f && within < .7f) {
    g.onto = true;
    g.gap = cell;
    return g;
  }
  g.gap = std::min(count, cell + (within >= .7f ? 1 : 0));
  return g;
}

// The drop as a request: the carried id goes before `before` ("" for the
// end). False when the drop changes nothing.
inline bool launcherDropBefore(const std::vector<std::string> &ids, const std::string &moved, int gap,
                               std::string &before) {
  const auto at = std::find(ids.begin(), ids.end(), moved);
  if (at == ids.end() || gap < 0 || gap > int(ids.size())) return false;
  const int from = int(at - ids.begin());
  if (gap == from || gap == from + 1) return false;
  before = gap < int(ids.size()) ? ids[gap] : "";
  return true;
}

// The same move the companion makes, applied at once so the grid does not
// jump back while the new catalogue is on its way.
inline bool launcherMove(LauncherCatalog &c, const std::string &moved, const std::string &before,
                         const std::string &folder) {
  if (folder.empty()) {
    auto id = [&](const LauncherItem &i) { return i.folder ? "folder:" + i.id : c.apps[i.app].id; };
    auto from = std::find_if(c.items.begin(), c.items.end(), [&](const auto &i) { return id(i) == moved; });
    if (from == c.items.end() || moved == before) return false;
    if (!before.empty() &&
        std::none_of(c.items.begin(), c.items.end(), [&](const auto &i) { return id(i) == before; }))
      return false;
    auto item = *from;
    c.items.erase(from);
    auto to = before.empty() ? c.items.end()
                             : std::find_if(c.items.begin(), c.items.end(), [&](const auto &i) { return id(i) == before; });
    c.items.insert(to, item);
    return true;
  }
  for (auto &item : c.items)
    if (item.folder && item.id == folder) {
      const int app = c.app(moved), next = before.empty() ? -1 : c.app(before);
      auto from = std::find(item.apps.begin(), item.apps.end(), app);
      if (app < 0 || from == item.apps.end() || moved == before ||
          (!before.empty() && std::find(item.apps.begin(), item.apps.end(), next) == item.apps.end()))
        return false;
      item.apps.erase(from);
      item.apps.insert(before.empty() ? item.apps.end() : std::find(item.apps.begin(), item.apps.end(), next), app);
      return true;
    }
  return false;
}

// Thumbstick paging: one page per push past 60%, re-armed below 30%.
struct LauncherStick {
  bool armed = true;
  int update(float x) {
    if (!std::isfinite(x)) return 0;
    if (armed && std::fabs(x) > .6f) {
      armed = false;
      return x > 0 ? 1 : -1;
    }
    if (std::fabs(x) < .3f) armed = true;
    return 0;
  }
};

// The host's side of the launcher: open state (a local gesture stays
// authoritative until the companion acknowledges it or starts a newer
// sequence, as the switcher does), where it is, the page and open folder, and
// a carried icon.
struct LauncherView {
  bool open = false;
  int desired = -1;
  int64_t requestSeq = 0;
  HmdMatrix34_t pose = shelfIdentity(), dimPose = shelfIdentity();
  int page = 0;
  std::string folder;
  int pageStepSeen = 0;
  std::string querySeen;
  int selectedSeen = -1;
  struct Carry {
    int hand = -1;
    std::string id, container; // what moves, and in which list ("" the top level)
    float x = 0, y = 0;        // the laser in panel units
    bool onPanel = false;
    LauncherGap gap;
    int edge = 0;
    double edgeSince = 0;
    bool active() const { return hand >= 0; }
  } carry;

  // A headset gesture: the caller sets the state's open flag and asks the companion.
  void request(LauncherState &s, bool next) {
    desired = next ? 1 : 0;
    requestSeq = s.seq;
    s.open = next;
  }
  // A newly read companion state: the local gesture holds until acknowledged.
  void acknowledge(LauncherState &next) {
    if (desired < 0) return;
    if (next.seq != requestSeq || next.open == bool(desired)) desired = -1;
    else next.open = bool(desired);
  }
  // Every frame; returns true when the launcher just opened.
  bool update(LauncherState &s, bool headValid, const HmdMatrix34_t &head) {
    const bool opened = s.open && !open;
    if (opened) {
      if (!headValid) {
        s.open = false; // placed only in front of a known head
        return false;
      }
      place(head);
      page = 0;
      folder.clear();
      pageStepSeen = s.pageStep;
      querySeen = s.query;
      selectedSeen = s.selected;
    }
    if (!s.open && open) close();
    open = s.open;
    if (!open) return false;
    // The Mac's arrows with an empty field turn pages; a new query starts at
    // its first page; the keyboard selection keeps its page in view.
    if (s.pageStep != pageStepSeen) {
      page += s.pageStep - pageStepSeen;
      pageStepSeen = s.pageStep;
    }
    if (s.query != querySeen) {
      querySeen = s.query;
      page = 0;
      selectedSeen = -1;
    }
    if (s.searching() && s.selected != selectedSeen) {
      selectedSeen = s.selected;
      page = s.selected / launcherPerPage;
    }
    return opened;
  }
  void place(const HmdMatrix34_t &head) {
    pose = shelfFrontOf(head, launcherDistance);
    pose.m[1][3] -= launcherDrop;
    dimPose = shelfFrontOf(head, launcherDimDistance);
  }
  void close() {
    open = false;
    folder.clear();
    page = 0;
    carry = {};
  }
  void clampPage(int pages) { page = std::clamp(page, 0, std::max(0, pages - 1)); }
};

// Everything the panel shows, for the painter (launcher_render.hpp). Panel
// units; icon paths are the companion's icons/<bundle>.png.
struct LauncherPicture {
  struct Tile {
    std::string name, icon;
    std::vector<std::string> mini; // a folder's first icons
    bool folder = false, hot = false, pressed = false, selected = false, ghost = false;
    float cx = 0, top = 0, size = 0;
  };
  std::string query, section;
  bool keyboard = false, searching = false, inFolder = false, backHot = false, backPressed = false, empty = false;
  std::vector<Tile> recent, grid;
  int page = 0, pages = 1, hotDot = -1;
  float markerX = -1, markerTop = 0;
  std::string signature() const {
    std::string out = query + '\x1f' + section + '\x1f' + char('0' + keyboard) + char('0' + searching) +
                      char('0' + inFolder) + char('0' + backHot) + char('0' + backPressed) + char('0' + empty) +
                      std::to_string(page) + '/' + std::to_string(pages) + ':' + std::to_string(hotDot) + ':' +
                      std::to_string(int(markerX * 10)) + ',' + std::to_string(int(markerTop));
    for (const auto *row : {&recent, &grid})
      for (const auto &t : *row) {
        out += '\x1e' + t.name + '\x1f' + t.icon + char('0' + t.folder) + char('0' + t.hot) + char('0' + t.pressed) +
               char('0' + t.selected) + char('0' + t.ghost) + std::to_string(int(t.cx)) + ',' + std::to_string(int(t.top));
        for (const auto &m : t.mini) out += '\x1d' + m;
      }
    return out;
  }
};

// The panel as it should look now. `hover` is what the laser (or the eyes)
// is on; `pressed` while the trigger holds it.
inline LauncherPicture launcherPicture(const LauncherCatalog &c, const LauncherState &s, LauncherView &v,
                                       const LauncherTarget &hover, bool pressed, const std::string &icons) {
  LauncherPicture p;
  auto iconPath = [&](int app) { return icons + "/" + c.apps[app].id + ".png"; };
  const auto listing = launcherListing(c, s, v.folder);
  v.clampPage(listing.pages());
  p.page = v.page;
  p.pages = listing.pages();
  p.query = s.query;
  p.keyboard = s.keyboard;
  p.searching = listing.searching;
  p.inFolder = listing.inFolder;
  p.section = listing.searching ? "Matching Applications" : listing.inFolder ? listing.folderName : "All Applications";
  p.empty = listing.searching && listing.cells.empty();
  p.backHot = hover.kind == LauncherTarget::Back;
  p.backPressed = p.backHot && pressed;
  p.hotDot = hover.kind == LauncherTarget::Dot ? hover.index : -1;
  const int recentCount = int(c.recent.size());
  for (int i = 0; i < recentCount; ++i) {
    LauncherPicture::Tile t;
    t.name = c.apps[c.recent[i]].name;
    t.icon = iconPath(c.recent[i]);
    t.cx = launcherRecentX(i, recentCount);
    t.top = launcherRecentTop;
    t.size = launcherRecentIcon;
    t.hot = !v.carry.active() && hover.kind == LauncherTarget::Recent && hover.index == i;
    t.pressed = t.hot && pressed;
    p.recent.push_back(t);
  }
  const int first = v.page * launcherPerPage;
  for (int slot = 0; slot < launcherPerPage && first + slot < int(listing.cells.size()); ++slot) {
    const auto &cell = listing.cells[first + slot];
    LauncherPicture::Tile t;
    launcherSlot(slot, t.cx, t.top);
    t.size = launcherIcon;
    if (cell.folder()) {
      const auto &item = c.items[cell.item];
      t.folder = true;
      t.name = item.name;
      for (size_t k = 0; k < item.apps.size() && k < 9; ++k) t.mini.push_back(iconPath(item.apps[k]));
    } else {
      t.name = c.apps[cell.app].name;
      t.icon = iconPath(cell.app);
    }
    t.hot = !v.carry.active() && hover.kind == LauncherTarget::Cell && hover.index == first + slot;
    t.pressed = t.hot && pressed;
    t.selected = listing.searching && s.selected == first + slot;
    t.ghost = v.carry.active() && launcherCellID(c, cell) == v.carry.id;
    p.grid.push_back(t);
  }
  // Where a carried icon would land: a bar in the gap.
  if (v.carry.active() && v.carry.gap) {
    const int count = int(listing.cells.size()), local = v.carry.gap.gap - first;
    float cx, top;
    if (local >= 0 && local <= launcherPerPage) {
      const bool after = local == launcherPerPage || (v.carry.gap.gap == count && local > 0);
      launcherSlot(after ? local - 1 : local, cx, top);
      p.markerX = cx + (after ? 1 : -1) * launcherCell / 2;
      p.markerTop = top;
    }
  }
  return p;
}

} // namespace fw
