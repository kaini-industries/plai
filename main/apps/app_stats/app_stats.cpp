/**
 * @file app_stats.cpp
 * @author d4rkmen
 * @brief Statistics widget - tabbed system info display
 * @version 2.0
 * @date 2025-01-03
 *
 * @copyright Copyright (c) 2025
 *
 */
#include "app_stats.h"
#include "common_define.h"
// #include "esp_log.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "freertos/task.h"
#include "esp_vfs_fat.h"
#include <time.h>
#include "apps/utils/ui/key_repeat.h"
#include "apps/utils/ui/draw_helper.h"
#include "mesh/mesh_service.h"
#include "mesh/node_db.h"
#include "meshtastic/portnums.pb.h"
#include <algorithm>
#include <format>
// assets
#include "assets/stat_system.h"
#include "assets/stat_radio.h"
#include "assets/stat_node.h"
#include "assets/stat_gps.h"
#include "assets/stat_mesh.h"
#include "assets/stat_db.h"
#include "assets/stat_tasks.h"

static const char* TAG __attribute__((unused)) = "APP_STATS";

#define UPDATE_INTERVAL_MS 2000
#define ROW_HEIGHT 14
#define BODY_START_Y 16
#define ICON_SIZE 12

static const char* TAB_NAMES[] = {"NODE", "SYSTEM", "RADIO", "NODE DB", "GPS", "MESH", "TASKS"};

static const char* HINT_STATS = "[\u2191][\u2193][\u2190][\u2192] [DEL] [ESC]";

static bool is_repeat = false;
static uint32_t next_fire_ts = 0xFFFFFFFF;

using namespace MOONCAKE::APPS;

void AppStats::onCreate()
{
    _data.hal = mcAppGetDatabase()->Get("HAL")->value<HAL::Hal*>();
    _data.current_tab = 0;
    _data.scroll_offset = 0;
    _data.scroll_max = 0;
    _data.last_update_ms = 0;
    _data.needs_redraw = true;
    hl_text_init(&_data.hint_hl_ctx, _data.hal->canvas(), 20, 1500);
}

void AppStats::onResume()
{
    ANIM_APP_OPEN();
    _data.hal->canvas()->fillScreen(THEME_COLOR_BG);
    _data.hal->canvas()->setFont(FONT_12);
    _data.hal->canvas()->setTextSize(1);
    _data.hal->canvas_update();

    _data.current_tab = 0;
    _data.scroll_offset = 0;
    _data.scroll_max = 0;
    _data.last_update_ms = 0;
    _data.needs_redraw = true;
}

void AppStats::onRunning()
{
    uint32_t now = millis();
    bool updated = false;

    if (_data.needs_redraw || now - _data.last_update_ms > UPDATE_INTERVAL_MS)
    {
        _render_tab();
        _data.last_update_ms = now;
        _data.needs_redraw = false;
        updated = true;
    }

    updated |= _render_hint();

    if (updated)
        _data.hal->canvas_update();

    _handle_input();
}

void AppStats::onDestroy() { hl_text_free(&_data.hint_hl_ctx); }

void AppStats::_render_tab()
{
    auto* canvas = _data.hal->canvas();
    canvas->fillScreen(THEME_COLOR_BG);

    _render_tab_header(TAB_NAMES[_data.current_tab]);

    _data.visible_rows = (canvas->height() - BODY_START_Y - 12) / ROW_HEIGHT;
    _data.row_idx = 0;
    _data.row_y = BODY_START_Y;

    switch (_data.current_tab)
    {
    case TAB_NODE:
        _render_node_info();
        break;
    case TAB_SYSTEM:
        _render_system_info();
        break;
    case TAB_RADIO:
        _render_radio_info();
        break;
    case TAB_NODEDB:
        _render_nodedb_info();
        break;
    case TAB_GPS:
        _render_gps_info();
        break;
    case TAB_MESH:
        _render_mesh_info();
        break;
    case TAB_TASKS:
        _render_tasks_info();
        break;
    }

    int total_rows = _data.row_idx;
    _data.scroll_max = total_rows > _data.visible_rows ? total_rows - _data.visible_rows : 0;
    if (_data.scroll_offset > _data.scroll_max)
        _data.scroll_offset = _data.scroll_max;

    UTILS::UI::draw_scrollbar(canvas,
                              canvas->width() - 3,
                              BODY_START_Y,
                              2,
                              _data.visible_rows * ROW_HEIGHT,
                              total_rows,
                              _data.visible_rows,
                              _data.scroll_offset);
}

void AppStats::_add_row(const char* label, const char* value, int color)
{
    if (_data.row_idx >= _data.scroll_offset && _data.row_idx < _data.scroll_offset + _data.visible_rows)
    {
        _draw_row(_data.row_y, label, value, color);
        _data.row_y += ROW_HEIGHT;
    }
    _data.row_idx++;
}

bool AppStats::_render_hint()
{
    return hl_text_render(&_data.hint_hl_ctx,
                          HINT_STATS,
                          0,
                          _data.hal->canvas()->height() - 9,
                          TFT_DARKGREY,
                          TFT_WHITE,
                          THEME_COLOR_BG);
}

void AppStats::_render_tab_header(const char* title)
{
    auto* canvas = _data.hal->canvas();
    const uint16_t* icons[] = {image_data_stat_node,
                               image_data_stat_system,
                               image_data_stat_radio,
                               image_data_stat_db,
                               image_data_stat_gps,
                               image_data_stat_mesh,
                               image_data_stat_tasks};
    if (_data.current_tab < TAB_COUNT)
    {
        canvas->pushImage(4, 1, ICON_SIZE, ICON_SIZE, icons[_data.current_tab]);
    }
    else
    {
        canvas->drawRect(4, 1, ICON_SIZE, ICON_SIZE, TFT_DARKGREY);
    }
    constexpr int dot_spacing = 10;
    constexpr int dot_r = 3;
    int dots_start_x = canvas->width() - TAB_COUNT * dot_spacing - 4;
    canvas->setFont(FONT_12);
    canvas->setTextColor(TFT_ORANGE, THEME_COLOR_BG);
    canvas->drawString(title, ICON_SIZE + 8, 1);
    for (int i = 0; i < TAB_COUNT; i++)
    {
        int cx = dots_start_x + i * dot_spacing + dot_r;
        int cy = 4 + dot_r;
        if (i == _data.current_tab)
            canvas->fillCircle(cx, cy, dot_r, TFT_ORANGE);
        else
            canvas->drawCircle(cx, cy, dot_r, TFT_DARKGREY);
    }

    canvas->drawFastHLine(0, BODY_START_Y - 2, canvas->width(), THEME_COLOR_HEADER_LINE);
}

void AppStats::_draw_row(int y, const char* label, const char* value, int value_color)
{
    auto* canvas = _data.hal->canvas();
    canvas->setFont(FONT_12);
    canvas->setTextColor(TFT_WHITE, THEME_COLOR_BG);
    canvas->drawString(label, 5, y);
    canvas->setTextColor(value_color, THEME_COLOR_BG);
    canvas->drawRightString(value, canvas->width() - 5, y);
}

// ========== Tab: Node Info ==========

void AppStats::_render_node_info()
{
    if (!_data.hal->mesh())
    {
        _add_row("Mesh", "Not initialized", TFT_RED);
        return;
    }

    const auto& config = _data.hal->mesh()->getConfig();
    char buf[48];

    snprintf(buf, sizeof(buf), "!%08lx", config.node_id);
    _add_row("Node ID", buf, TFT_CYAN);
    _add_row("Long Name", config.long_name, TFT_GREEN);
    _add_row("Short Name", config.short_name, TFT_GREEN);
    _add_row("Role", Mesh::NodeDB::getRoleName(config.role), TFT_YELLOW);
    _add_row("PKI", config.public_key_len == 32 ? "Enabled" : "None", config.public_key_len == 32 ? TFT_GREEN : TFT_DARKGREY);

    uint32_t remain_ms = _data.hal->mesh()->getNodeInfoBroadcastRemainingMs();
    uint32_t sec = remain_ms / 1000;
    snprintf(buf, sizeof(buf), "%02lum %02lus", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
    _add_row("Next NodeInfo", buf, TFT_CYAN);

    remain_ms = _data.hal->mesh()->getPositionBroadcastRemainingMs();
    sec = remain_ms / 1000;
    snprintf(buf, sizeof(buf), "%02lum %02lus", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
    _add_row("Next Position", buf, TFT_CYAN);

    remain_ms = _data.hal->mesh()->getTelemetryBroadcastRemainingMs();
    sec = remain_ms / 1000;
    snprintf(buf, sizeof(buf), "%02lum %02lus", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
    _add_row("Next Telemetry", buf, TFT_CYAN);
}

// ========== Tab: System Info ==========

void AppStats::_render_system_info()
{
    char buf[64];

    size_t total_heap = heap_caps_get_total_size(MALLOC_CAP_8BIT);
    uint32_t free_heap = esp_get_free_heap_size();
    snprintf(buf, sizeof(buf), "%u / %u KB", (unsigned)(free_heap / 1024), (unsigned)(total_heap / 1024));
    _add_row("Heap (free/total)", buf, TFT_CYAN);

    uint32_t min_heap = esp_get_minimum_free_heap_size();
    snprintf(buf, sizeof(buf), "%lu KB", min_heap / 1024);
    _add_row("Min Heap Ever", buf, min_heap < 20480 ? TFT_RED : TFT_CYAN);

#if BOARD_HAS_PSRAM
    size_t psram_free = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    size_t psram_total = heap_caps_get_total_size(MALLOC_CAP_SPIRAM);
    if (psram_total > 0)
    {
        snprintf(buf, sizeof(buf), "%u / %u KB", (unsigned)(psram_free / 1024), (unsigned)(psram_total / 1024));
        _add_row("PSRAM", buf, TFT_CYAN);
    }
#endif

    if (_data.hal->sdcard() && _data.hal->sdcard()->is_mounted())
    {
        uint64_t total_bytes = 0, free_bytes = 0;
        if (esp_vfs_fat_info("/sdcard", &total_bytes, &free_bytes) == ESP_OK && total_bytes > 0)
        {
            uint32_t total_mb = (uint32_t)(total_bytes / (1024 * 1024));
            uint32_t used_mb = (uint32_t)((total_bytes - free_bytes) / (1024 * 1024));
            snprintf(buf, sizeof(buf), "%lu / %lu MB", (unsigned long)used_mb, (unsigned long)total_mb);
            _add_row("Storage (used/total)", buf, TFT_CYAN);
        }
    }

    uint32_t uptime_ms = (uint32_t)millis();
    _add_row("Uptime", _format_uptime(uptime_ms).c_str(), TFT_CYAN);

    time_t now_t = time(nullptr);
    struct tm ti;
    localtime_r(&now_t, &ti);
    if (ti.tm_year > 100)
    {
        snprintf(buf,
                 sizeof(buf),
                 "%02d.%02d.%04d %02d:%02d:%02d",
                 ti.tm_mday,
                 ti.tm_mon + 1,
                 ti.tm_year + 1900,
                 ti.tm_hour,
                 ti.tm_min,
                 ti.tm_sec);
        _add_row("DateTime", buf, TFT_CYAN);
    }
    else
    {
        _add_row("DateTime", "Not set", TFT_DARKGREY);
    }

    float voltage = _data.hal->getBatVoltage();
    uint8_t level = _data.hal->getBatLevel(voltage);
    snprintf(buf, sizeof(buf), "%.2fV %u%%", voltage, level);
    _add_row("Battery", buf, level < 25 ? TFT_RED : level < 50 ? TFT_YELLOW : level < 75 ? TFT_GREEN : TFT_CYAN);

    _add_row("Version", BUILD_NUMBER, TFT_CYAN);
    _add_row("Built", __DATE__ " " __TIME__, TFT_DARKGREY);
}

// ========== Tab: Radio Info ==========

const char* AppStats::_preset_name(int preset)
{
    return Mesh::getPresetName(static_cast<meshtastic_Config_LoRaConfig_ModemPreset>(preset));
}

void AppStats::_render_radio_info()
{
    char buf[32];

    if (!_data.hal->mesh())
    {
        _add_row("Radio", "Not initialized", TFT_RED);
        return;
    }

    const auto& mesh_config = _data.hal->mesh()->getConfig();

    float freq = _data.hal->mesh()->getFrequency();
    snprintf(buf, sizeof(buf), "%.3f MHz", freq);
    _add_row("Freq", buf, TFT_CYAN);

    if (mesh_config.lora_config.use_preset)
        _add_row("Preset", _preset_name(mesh_config.lora_config.modem_preset), TFT_GREEN);

    if (_data.hal->radio())
    {
        auto radio_cfg = _data.hal->radio()->getConfig();
        snprintf(buf,
                 sizeof(buf),
                 "SF%u BW%.0f CR4/%u",
                 radio_cfg.spreading_factor,
                 radio_cfg.bandwidth_hz / 1000.0f,
                 radio_cfg.coding_rate);
        _add_row("Waveform", buf, TFT_CYAN);

        snprintf(buf, sizeof(buf), "%d dBm", radio_cfg.tx_power_dbm);
        _add_row("TX Power", buf, TFT_CYAN);
    }

    const auto& stats = Mesh::MeshDataStore::getInstance().getStats();
    snprintf(buf, sizeof(buf), "%lu", stats.rx_packets);
    _add_row("RX Packets", buf, TFT_CYAN);

    snprintf(buf, sizeof(buf), "%lu", stats.tx_packets);
    _add_row("TX Packets", buf, TFT_GREEN);
}

// ========== Tab: Node DB Info ==========

void AppStats::_render_nodedb_info()
{
    char buf[16];

    if (_data.hal->nodedb())
    {
        size_t online = _data.hal->nodedb()->getOnlineNodeCount();
        size_t total = _data.hal->nodedb()->getNodeCount();
        snprintf(buf, sizeof(buf), "%u / %u", (unsigned)online, (unsigned)total);
        _add_row("Nodes (online/total)", buf, TFT_CYAN);
    }

    snprintf(buf, sizeof(buf), "%u", (unsigned)Mesh::favorites_get_count());
    _add_row("Favorites", buf, TFT_YELLOW);

    snprintf(buf, sizeof(buf), "%u", (unsigned)Mesh::ignorelist_get_count());
    _add_row("Ignored", buf, Mesh::ignorelist_get_count() > 0 ? TFT_RED : TFT_DARKGREY);

    const auto& stats = Mesh::MeshDataStore::getInstance().getStats();
    snprintf(buf, sizeof(buf), "%lu", stats.messages_sent);
    _add_row("Msgs Sent", buf, TFT_GREEN);

    snprintf(buf, sizeof(buf), "%lu", stats.messages_received);
    _add_row("Msgs Recv", buf, TFT_CYAN);
}

// ========== Tab: GPS Info ==========

void AppStats::_render_gps_info()
{
    char buf[32];

#if HAL_USE_GPS
    auto* gps = _data.hal->gps();
    if (!gps || !gps->isInitialized())
    {
        _add_row("GPS", "Not available", TFT_DARKGREY);
        return;
    }

    auto data = gps->getData();

    static const char* fix_names[] = {"No Fix", "GPS", "DGPS", "PPS", "RTK", "FloatRTK", "Est", "Manual", "Sim"};
    int fix_idx = (int)data.fix_quality;
    if (fix_idx > 8)
        fix_idx = 0;
    _add_row("Fix", fix_names[fix_idx], data.has_fix ? TFT_GREEN : TFT_RED);

    snprintf(buf, sizeof(buf), "%lu / %lu", (unsigned long)data.sats_used, (unsigned long)data.sats_in_view);
    _add_row("Satellites (used/in view)", buf, data.sats_used > 0 ? TFT_CYAN : TFT_DARKGREY);

    if (data.has_fix)
    {
        snprintf(buf, sizeof(buf), "%.7f", data.latitude);
        _add_row("Latitude", buf, TFT_CYAN);

        snprintf(buf, sizeof(buf), "%.7f", data.longitude);
        _add_row("Longitude", buf, TFT_CYAN);

        snprintf(buf, sizeof(buf), "%d / %d m", (int)data.altitude_msl, (int)data.altitude_hae);
        _add_row("Altitude (MSL / HAE)", buf, TFT_CYAN);

        snprintf(buf, sizeof(buf), "%.1f", data.hdop / 100.0f);
        _add_row("HDOP (precision)", buf, data.hdop < 200 ? TFT_GREEN : TFT_YELLOW);
    }
    else
    {
        snprintf(buf, sizeof(buf), "%.1f", data.hdop / 100.0f);
        _add_row("HDOP", buf, TFT_DARKGREY);

        snprintf(buf, sizeof(buf), "%lu", data.sentence_count);
        _add_row("NMEA Msgs", buf, TFT_DARKGREY);
    }
#else
    _add_row("GPS", "Not supported", TFT_DARKGREY);
#endif
}

// ========== Tab: Mesh Port Distribution ==========

const char* AppStats::_port_name(uint8_t port)
{
    switch (port)
    {
    case meshtastic_PortNum_TEXT_MESSAGE_APP:
        return "Text";
    case meshtastic_PortNum_POSITION_APP:
        return "Position";
    case meshtastic_PortNum_NODEINFO_APP:
        return "NodeInfo";
    case meshtastic_PortNum_TELEMETRY_APP:
        return "Telemetry";
    case meshtastic_PortNum_ROUTING_APP:
        return "Routing";
    case meshtastic_PortNum_ADMIN_APP:
        return "Admin";
    case meshtastic_PortNum_TRACEROUTE_APP:
        return "Traceroute";
    case meshtastic_PortNum_WAYPOINT_APP:
        return "Waypoint";
    case meshtastic_PortNum_NEIGHBORINFO_APP:
        return "Neighbor";
    case meshtastic_PortNum_STORE_FORWARD_APP:
        return "Store&Fwd";
    case meshtastic_PortNum_RANGE_TEST_APP:
        return "RangeTest";
    case meshtastic_PortNum_MAP_REPORT_APP:
        return "MapReport";
    case meshtastic_PortNum_DETECTION_SENSOR_APP:
        return "Sensor";
    case meshtastic_PortNum_REMOTE_HARDWARE_APP:
        return "RemoteHW";
    case meshtastic_PortNum_ATAK_PLUGIN:
        return "ATAK";
    case meshtastic_PortNum_SERIAL_APP:
        return "Serial";
    case meshtastic_PortNum_PAXCOUNTER_APP:
        return "PaxCount";
    case meshtastic_PortNum_TEXT_MESSAGE_COMPRESSED_APP:
        return "TextComp";
    case meshtastic_PortNum_AUDIO_APP:
        return "Audio";
    case meshtastic_PortNum_REPLY_APP:
        return "Reply";
    case meshtastic_PortNum_IP_TUNNEL_APP:
        return "IPTunnel";
    case meshtastic_PortNum_STORE_FORWARD_PLUSPLUS_APP:
        return "S&F++";
    case meshtastic_PortNum_SIMULATOR_APP:
        return "Simulator";
    case meshtastic_PortNum_POWERSTRESS_APP:
        return "PwrStress";
    case meshtastic_PortNum_UNKNOWN_APP:
        return "Unknown";
    default:
        return nullptr;
    }
}

void AppStats::_render_mesh_info()
{
    const auto& pd = Mesh::MeshDataStore::getInstance().getPortDistribution();
    const auto& stats = Mesh::MeshDataStore::getInstance().getStats();

    if (pd.rx_total == 0 && stats.tx_packets == 0)
    {
        _add_row("Packets", "No data", TFT_DARKGREY);
        return;
    }

    struct SortEntry
    {
        uint8_t port;
        bool is_crc;
        uint32_t count;
    };

    SortEntry sorted[Mesh::PORT_STATS_MAX + 1];
    int sorted_count = 0;

    for (int i = 0; i < pd.count && sorted_count < Mesh::PORT_STATS_MAX; i++)
        sorted[sorted_count++] = {pd.entries[i].port, false, pd.entries[i].rx_count};
    if (pd.crc_errors > 0 && sorted_count < Mesh::PORT_STATS_MAX + 1)
        sorted[sorted_count++] = {0, true, pd.crc_errors};

    std::sort(sorted, sorted + sorted_count, [](const SortEntry& a, const SortEntry& b) { return a.count > b.count; });

    char buf[32];
    snprintf(buf, sizeof(buf), "RX:%lu TX:%lu", (unsigned long)pd.rx_total, (unsigned long)stats.tx_packets);
    _add_row("Total", buf, TFT_ORANGE);

    for (int i = 0; i < sorted_count; i++)
    {
        const char* name;
        char name_buf[16];
        if (sorted[i].is_crc)
        {
            name = "CRC Error";
        }
        else
        {
            name = _port_name(sorted[i].port);
            if (!name)
            {
                snprintf(name_buf, sizeof(name_buf), "Port %d", sorted[i].port);
                name = name_buf;
            }
        }

        float pct = pd.rx_total > 0 ? (sorted[i].count * 100.0f / pd.rx_total) : 0;
        char val[24];
        snprintf(val, sizeof(val), "%lu (%.1f%%)", (unsigned long)sorted[i].count, pct);

        int color;
        if (sorted[i].is_crc)
            color = TFT_RED;
        else if (pct > 30.0f)
            color = TFT_GREEN;
        else if (pct > 10.0f)
            color = TFT_CYAN;
        else
            color = TFT_DARKGREY;

        _add_row(name, val, color);
    }
}

// ========== Tab: FreeRTOS Tasks ==========

void AppStats::_render_tasks_info()
{
    UBaseType_t task_count = uxTaskGetNumberOfTasks();
    if (task_count == 0)
    {
        _add_row("Tasks", "None", TFT_DARKGREY);
        return;
    }

    constexpr int MAX_TASKS = 32;
    TaskStatus_t tasks[MAX_TASKS];
    uint32_t total_runtime;
    UBaseType_t filled = uxTaskGetSystemState(tasks, MAX_TASKS, &total_runtime);

    std::sort(tasks,
              tasks + filled,
              [](const TaskStatus_t& a, const TaskStatus_t& b) { return a.uxCurrentPriority > b.uxCurrentPriority; });

    std::string tasks_count = std::format("Total count: {}", (unsigned)filled);
    _add_row(tasks_count.c_str(), "Core Pri Stack", TFT_ORANGE);

    for (UBaseType_t i = 0; i < filled; i++)
    {
#ifdef CONFIG_FREERTOS_VTASKLIST_INCLUDE_COREID
        int core = tasks[i].xCoreID;
        const char* core_str = core == 0 ? "#0" : core == 1 ? "#1" : "";
#else
        const char* core_str = "";
#endif
        std::string task_descr = std::format("{:2s}  {:2d} {:4d}B",
                                             core_str,
                                             (unsigned)tasks[i].uxCurrentPriority,
                                             (unsigned long)tasks[i].usStackHighWaterMark);
        int color;
        if (tasks[i].usStackHighWaterMark < 512)
            color = TFT_RED;
        else if (tasks[i].usStackHighWaterMark < 1024)
            color = TFT_YELLOW;
        else
            color = TFT_CYAN;

        _add_row(tasks[i].pcTaskName, task_descr.c_str(), color);
    }
}

// ========== Input Handling ==========

void AppStats::_handle_input()
{
    _data.hal->keyboard()->updateKeyList();
    _data.hal->keyboard()->updateKeysState();

    if (_data.hal->keyboard()->isPressed())
    {
        uint32_t now = millis();

        if (_data.hal->keyboard()->isKeyPressing(KEY_NUM_ESC) || _data.hal->keyboard()->isKeyPressing(KEY_NUM_BACKSPACE) ||
            _data.hal->home_button()->is_pressed())
        {
            _data.hal->playNextSound();
            _data.hal->keyboard()->waitForRelease(KEY_NUM_ESC);
            destroyApp();
            return;
        }
        else if (_data.hal->keyboard()->isKeyPressing(KEY_NUM_RIGHT))
        {
            if (key_repeat_check(is_repeat, next_fire_ts, now))
            {
                _data.hal->playNextSound();
                _data.current_tab = (_data.current_tab + 1) % TAB_COUNT;
                _data.scroll_offset = 0;
                _data.needs_redraw = true;
            }
        }
        else if (_data.hal->keyboard()->isKeyPressing(KEY_NUM_LEFT))
        {
            if (key_repeat_check(is_repeat, next_fire_ts, now))
            {
                _data.hal->playNextSound();
                _data.current_tab = (_data.current_tab + TAB_COUNT - 1) % TAB_COUNT;
                _data.scroll_offset = 0;
                _data.needs_redraw = true;
            }
        }
        else if (_data.hal->keyboard()->isKeyPressing(KEY_NUM_DOWN))
        {
            if (key_repeat_check(is_repeat, next_fire_ts, now))
            {
                if (_data.scroll_offset < _data.scroll_max)
                {
                    _data.hal->playNextSound();
                    _data.scroll_offset++;
                    _data.needs_redraw = true;
                }
            }
        }
        else if (_data.hal->keyboard()->isKeyPressing(KEY_NUM_UP))
        {
            if (key_repeat_check(is_repeat, next_fire_ts, now))
            {
                if (_data.scroll_offset > 0)
                {
                    _data.hal->playNextSound();
                    _data.scroll_offset--;
                    _data.needs_redraw = true;
                }
            }
        }
    }
    else
    {
        is_repeat = false;
    }
}

// ========== Helpers ==========

std::string AppStats::_format_uptime(uint32_t ms)
{
    uint32_t secs = ms / 1000;
    uint32_t mins = secs / 60;
    uint32_t hours = mins / 60;
    uint32_t days = hours / 24;

    char buf[32];
    if (days > 0)
        snprintf(buf, sizeof(buf), "%dd %dh %dm", (int)days, (int)(hours % 24), (int)(mins % 60));
    else if (hours > 0)
        snprintf(buf, sizeof(buf), "%dh %dm %ds", (int)hours, (int)(mins % 60), (int)(secs % 60));
    else if (mins > 0)
        snprintf(buf, sizeof(buf), "%dm %ds", (int)mins, (int)(secs % 60));
    else
        snprintf(buf, sizeof(buf), "%ds", (int)secs);
    return buf;
}
