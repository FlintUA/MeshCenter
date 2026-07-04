from flask import request, jsonify, Response, send_from_directory


def register_camera_routes(app, camera, handle_errors):
    @app.route('/video_feed')
    def video_feed():
        """MJPEG видео поток"""
        if not camera.CAMERA_AVAILABLE:
            print("[CAMERA] ❌ Camera not available", flush=True)
            return "Camera not available", 503

        return Response(
            camera.generate_mjpeg_stream(),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )

    @app.route("/api/camera/status")
    def api_camera_status():
        """Статус камеры"""
        return jsonify(camera.get_camera_status())

    @app.route("/api/camera/settings", methods=["GET"])
    def api_camera_settings():
        """Получить текущие настройки видео"""
        return jsonify(camera.get_camera_settings())

    @app.route("/api/camera/settings", methods=["POST"])
    @handle_errors
    def api_camera_update_settings():
        """Обновить настройки камеры"""
        data = request.get_json(force=True)
        result, status = camera.update_camera_settings(data)
        return jsonify(result), status

    @app.route("/api/camera/stop", methods=["POST"])
    @handle_errors
    def api_camera_stop():
        """Полностью остановить камеру"""
        camera.stop_camera()
        return jsonify({
            "ok": True,
            "mode": camera.CAMERA_MODE,
            "started": camera.camera_started
        })

    @app.route("/api/camera/switch_mode", methods=["POST"])
    @handle_errors
    def api_camera_switch_mode():
        """Переключение режима камеры"""
        data = request.get_json(force=True)
        result, status = camera.api_switch_mode(data)
        return jsonify(result), status

    @app.route("/api/camera/mode/<mode>", methods=["POST"])
    def api_camera_set_mode(mode):
        """Переключить предустановленный режим"""
        result, status = camera.set_video_mode(mode)
        return jsonify(result), status

    @app.route("/api/camera/screenshot", methods=["POST"])
    @handle_errors
    def api_camera_screenshot():
        """Создать скриншот"""
        result = camera.capture_screenshot()

        if result.get("success") or result.get("ok"):
            result["ok"] = True
            return jsonify(result)

        return jsonify({
            "ok": False,
            "error": result.get("error", "Unknown error")
        }), 500

    @app.route("/api/camera/screenshot/<path:filename>")
    def api_camera_screenshot_file(filename):
        """Получить скриншот"""
        if not camera.screenshot_exists(filename):
            return jsonify({"ok": False, "error": "File not found"}), 404

        return send_from_directory(
            camera.SCREENSHOTS_DIR,
            filename,
            mimetype="image/jpeg"
        )

    @app.route("/api/camera/screenshots", methods=["GET"])
    def api_camera_screenshots_list():
        """Список всех скриншотов"""
        result, status = camera.list_screenshots()
        return jsonify(result), status

    @app.route("/api/camera/screenshot/<path:filename>", methods=["DELETE"])
    @handle_errors
    def api_camera_screenshot_delete(filename):
        """Удалить скриншот"""
        result, status = camera.delete_screenshot(filename)
        return jsonify(result), status

    @app.route("/api/camera/screenshots", methods=["DELETE"])
    @handle_errors
    def api_camera_screenshots_delete_all():
        """Удалить все скриншоты"""
        result, status = camera.delete_all_screenshots()
        return jsonify(result), status

    @app.route("/api/photo/settings", methods=["GET"])
    def api_photo_settings():
        """Получить настройки фото"""
        return jsonify(camera.get_photo_settings())

    @app.route("/api/photo/settings", methods=["POST"])
    @handle_errors
    def api_photo_update_settings():
        """Обновить настройки фото"""
        data = request.get_json(force=True)
        result, status = camera.update_photo_settings(data)
        return jsonify(result), status

    @app.route("/api/photo/capture", methods=["POST"])
    @handle_errors
    def api_photo_capture():
        """Захват фото для превью"""
        result, status = camera.capture_photo_preview()
        return jsonify(result), status

    @app.route("/api/photo/save", methods=["POST"])
    @handle_errors
    def api_photo_save():
        """Сохранить фото в максимальном качестве"""
        result, status = camera.save_highres_photo()
        return jsonify(result), status
