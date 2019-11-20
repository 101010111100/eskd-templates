import sys
import uno
import unohelper
from com.sun.star.util import XModifyListener
import zipimport

EMBEDDED_MODULES = (
    "textwidth",
    "kicadnet",
    "config",
    "schematic",
    "common",
)

# Декларация встроенных модулей. Они будут импортированы позже.
common = None
config = None
textwidth = None


class DocModifyListener(unohelper.Base, XModifyListener):
    """Класс для прослушивания изменений в документе."""

    def __init__(self,):
        doc = XSCRIPTCONTEXT.getDocument()
        self.prevFirstPageStyleName = doc.Text.createTextCursor().PageDescName
        self.prevTableRowCount = doc.TextTables["Спецификация"].Rows.Count
        self.prevPageCount = doc.CurrentController.PageCount

    def modified(self, event):
        """Приём сообщения об изменении в документе."""
        if common.SKIP_MODIFY_EVENTS:
            return
        doc = event.Source
        # Чтобы избежать рекурсивного зацикливания,
        # необходимо сначала удалить, а после изменений,
        # снова добавить обработчик сообщений об изменениях.
        doc.removeModifyListener(self)
        doc.UndoManager.lock()

        firstPageStyleName = doc.Text.createTextCursor().PageDescName
        if firstPageStyleName and "Спецификация" in doc.TextTables:
            table = doc.TextTables["Спецификация"]
            tableRowCount = table.Rows.Count
            if firstPageStyleName != self.prevFirstPageStyleName \
                or tableRowCount != self.prevTableRowCount:
                    self.prevFirstPageStyleName = firstPageStyleName
                    self.prevTableRowCount = tableRowCount
                    if not common.isThreadWorking():
                        # Высота строк подстраивается автоматически так, чтобы нижнее
                        # обрамление последней строки листа совпадало с верхней линией
                        # основной надписи.
                        # Данное действие выполняется только при редактировании таблицы
                        # спецификации вручную.
                        # При автоматическом построении специф. высота строк и таблица
                        # регистрации изменений обрабатываются отдельным образом
                        # (см. spec.py).
                        doc.lockControllers()
                        for rowIndex in range(1, tableRowCount):
                            table.Rows[rowIndex].Height = common.getSpecRowHeight(rowIndex)
                        doc.unlockControllers()

                        # Автоматическое добавление/удаление
                        # таблицы регистрации изменений.
                        pageCount = doc.CurrentController.PageCount
                        if pageCount != self.prevPageCount:
                            self.prevPageCount = pageCount
                            if config.getboolean("spec", "append rev table"):
                                if "Лист_регистрации_изменений" in doc.TextTables:
                                    pageCount -= 1
                                if pageCount > config.getint("spec", "pages rev table"):
                                    if common.appendRevTable():
                                        self.prevPageCount += 1
                                else:
                                    if common.removeRevTable():
                                        self.prevPageCount -= 1

        if not common.isThreadWorking():
            currentTable = doc.CurrentController.ViewCursor.TextTable
            currentCell = doc.CurrentController.ViewCursor.Cell
            currentFrame = doc.CurrentController.ViewCursor.TextFrame
            if currentCell or currentFrame:
                if currentCell:
                    itemName = currentCell.createTextCursor().ParaStyleName
                    item = currentCell
                else: # currentFrame
                    itemName = currentFrame.Name
                    item = currentFrame
                itemCursor = item.createTextCursor()
                for name in common.ITEM_WIDTHS:
                    if itemName.endswith(name):
                        itemWidth = common.ITEM_WIDTHS[name]
                        if itemName == "Поз." \
                            and currentTable.Name == "Спецификация":
                                # Подстроить ширину всех позиционных номеров
                                # при изменении хотя бы одного.
                                doc.TextFields.refresh()
                                for row in range(1, currentTable.Rows.Count):
                                    cellPos = currentTable.getCellByName(
                                        "C{}".format(row + 1)
                                    )
                                    for textContent in cellPos:
                                        widthFactor = textwidth.getWidthFactor(
                                            cellPos.String,
                                            textContent.CharHeight,
                                            itemWidth - 1
                                        )
                                        textContent.CharScaleWidth = widthFactor
                        else:
                            for line in item.String.splitlines(keepends=True):
                                widthFactor = textwidth.getWidthFactor(
                                    line,
                                    itemCursor.CharHeight,
                                    itemWidth - 1
                                )
                                itemCursor.goRight(len(line), True)
                                itemCursor.CharScaleWidth = widthFactor
                                itemCursor.collapseToEnd()

            if currentFrame is not None \
                and currentFrame.Name.startswith("1.") \
                and not currentFrame.Name.endswith(".7 Лист") \
                and not currentFrame.Name.endswith(".8 Листов"):
                    # Обновить только текущую графу
                    name = currentFrame.Name[4:]
                    text = currentFrame.String
                    cursor = currentFrame.createTextCursor()
                    fontSize = cursor.CharHeight
                    widthFactor = cursor.CharScaleWidth
                    # Есть 4 варианта оформления первого листа
                    # в виде 4-х стилей страницы.
                    # Поля форматной рамки хранятся в нижнем колонтитуле
                    # и для каждого стиля имеется свой набор полей.
                    # При редактировании, значения полей нужно синхронизировать
                    # между собой.
                    for firstPageVariant in "1234":
                        if currentFrame.Name[2] == firstPageVariant:
                            continue
                        otherName = "1.{}.{}".format(firstPageVariant, name)
                        if otherName in doc.TextFrames:
                            otherFrame = doc.TextFrames[otherName]
                            otherFrame.String = text
                            otherCursor = otherFrame.createTextCursor()
                            otherCursor.gotoEnd(True)
                            otherCursor.CharHeight = fontSize
                            otherCursor.CharScaleWidth = widthFactor
                    # А также, обновить поля на последующих листах
                    if name in common.STAMP_COMMON_FIELDS:
                        otherFrame = doc.TextFrames["N." + name]
                        otherFrame.String = text
                        otherCursor = otherFrame.createTextCursor()
                        otherCursor.gotoEnd(True)
                        otherCursor.CharHeight = fontSize
                        if name.endswith("2 Обозначение документа") \
                            and widthFactor < 100:
                                widthFactor *= 110 / 120
                        otherCursor.CharScaleWidth = widthFactor

        doc.UndoManager.unlock()
        doc.addModifyListener(self)


def importEmbeddedModules(*args):
    """Импорт встроенных в документ модулей.

    При создании нового документа из шаблона, его сразу же нужно сохранить,
    чтобы получить доступ к содержимому.
    Встроенные модули импортируются с помощью стандартного модуля zipimport.

    """
    doc = XSCRIPTCONTEXT.getDocument()
    if not doc.URL:
        context = XSCRIPTCONTEXT.getComponentContext()
        frame = doc.CurrentController.Frame

        filePicker = context.ServiceManager.createInstanceWithContext(
            "com.sun.star.ui.dialogs.OfficeFilePicker",
            context
        )
        filePicker.setTitle("Сохранение новой спецификации")
        pickerType = uno.getConstantByName(
            "com.sun.star.ui.dialogs.TemplateDescription.FILESAVE_SIMPLE"
        )
        filePicker.initialize((pickerType,))
        path = context.ServiceManager.createInstanceWithContext(
            "com.sun.star.util.PathSubstitution",
            context
        )
        homeDir = path.getSubstituteVariableValue("$(work)")
        filePicker.setDisplayDirectory(homeDir)
        filePicker.setDefaultName("Спецификация.odt")
        result = filePicker.execute()
        OK = uno.getConstantByName(
            "com.sun.star.ui.dialogs.ExecutableDialogResults.OK"
        )
        if result == OK:
            fileUrl = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
            fileUrl.Name = "URL"
            fileUrl.Value = filePicker.Files[0]

            dispatchHelper = context.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.DispatchHelper",
                context
            )
            dispatchHelper.executeDispatch(
                frame,
                ".uno:SaveAs",
                "",
                0,
                (fileUrl,)
            )
        if not doc.URL:
            msgbox = frame.ContainerWindow.Toolkit.createMessageBox(
                frame.ContainerWindow,
                uno.Enum("com.sun.star.awt.MessageBoxType", "MESSAGEBOX"),
                uno.getConstantByName("com.sun.star.awt.MessageBoxButtons.BUTTONS_YES_NO"),
                "Внимание!",
                "Для работы макросов необходимо сначала сохранить документ.\n"
                "Продолжить?"
            )
            yes = uno.getConstantByName("com.sun.star.awt.MessageBoxResults.YES")
            result = msgbox.execute()
            if result == yes:
                return importEmbeddedModules()
            return False
    docPath = uno.fileUrlToSystemPath(doc.URL)
    docId = doc.RuntimeUID
    modulePath = docPath + "/Scripts/python/pythonpath/"
    importer = zipimport.zipimporter(modulePath)
    for moduleName in EMBEDDED_MODULES:
        if moduleName in sys.modules:
            # Если модуль с таким же именем был загружен ранее,
            # его необходимо удалить из списка системы импорта,
            # чтобы в последующем модуль был загружен строго из
            # указанного места.
            del sys.modules[moduleName]
        module = importer.load_module(moduleName)
        module.__name__ = moduleName + docId
        module.init(XSCRIPTCONTEXT)
        del sys.modules[moduleName]
        sys.modules[moduleName + docId] = module
    global common
    common = sys.modules["common" + docId]
    global config
    config = sys.modules["config" + docId]
    global textwidth
    textwidth = sys.modules["textwidth" + docId]
    return True


def init(*args):
    """Начальная настройка при открытии документа."""
    context = XSCRIPTCONTEXT.getComponentContext()
    dispatchHelper = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.DispatchHelper",
        context
    )
    doc = XSCRIPTCONTEXT.getDocument()
    frame = doc.CurrentController.Frame
    if not importEmbeddedModules():
        dispatchHelper.executeDispatch(
            frame,
            ".uno:CloseDoc",
            "",
            0,
            ()
        )
        return
    config.load()
    if "Спецификация" not in doc.TextTables:
        common.rebuildTable()
    listener = DocModifyListener()
    doc.addModifyListener(listener)
    if config.getboolean("settings", "set view options"):
        options = (
            {
                "path": "/org.openoffice.Office.Writer/Content/NonprintingCharacter",
                "prop": "HiddenParagraph",
                "value": False,
                "command": ".uno:ShowHiddenParagraphs"
            },
            {
                "path": "/org.openoffice.Office.UI/ColorScheme/ColorSchemes/org.openoffice.Office.UI:ColorScheme['LibreOffice']/DocBoundaries",
                "prop": "IsVisible",
                "value": False,
                "command": ".uno:ViewBounds"
            },
            {
                "path": "/org.openoffice.Office.UI/ColorScheme/ColorSchemes/org.openoffice.Office.UI:ColorScheme['LibreOffice']/TableBoundaries",
                "prop": "IsVisible",
                "value": False,
                "command": ".uno:TableBoundaries"
            },
            {
                "path": "/org.openoffice.Office.UI/ColorScheme/ColorSchemes/org.openoffice.Office.UI:ColorScheme['LibreOffice']/WriterFieldShadings",
                "prop": "IsVisible",
                "value": False,
                "command": ".uno:Marks"
            },
            {
                "path": "/org.openoffice.Office.Common/Help",
                "prop": "ExtendedTip",
                "value": True,
                "command": ".uno:ActiveHelp"
            },
        )
        configProvider = context.ServiceManager.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider",
            context
        )
        nodePath = uno.createUnoStruct("com.sun.star.beans.PropertyValue")
        nodePath.Name = "nodepath"
        for op in options:
            nodePath.Value = op["path"]
            configAccess = configProvider.createInstanceWithArguments(
                "com.sun.star.configuration.ConfigurationAccess",
                (nodePath,)
            )
            value = configAccess.getPropertyValue(op["prop"])
            if value != op["value"]:
                dispatchHelper.executeDispatch(
                    frame,
                    op["command"],
                    "",
                    0,
                    ()
                )
        toolbarPos = frame.LayoutManager.getElementPos(
            "private:resource/toolbar/custom_spec"
        )
        if toolbarPos.X == 0 and toolbarPos.Y == 0:
            toolbarPos.Y = 2147483647
            frame.LayoutManager.dockWindow(
                "private:resource/toolbar/custom_spec",
                uno.Enum("com.sun.star.ui.DockingArea", "DOCKINGAREA_DEFAULT"),
                toolbarPos
            )

def cleanup(*args):
    """Удалить объекты встроенных модулей из системы импорта Python."""

    for moduleName in EMBEDDED_MODULES:
        moduleName += XSCRIPTCONTEXT.getDocument().RuntimeUID
        if moduleName in sys.modules:
            del sys.modules[moduleName]
    docPath = uno.fileUrlToSystemPath(XSCRIPTCONTEXT.getDocument().URL)
    if docPath in zipimport._zip_directory_cache:
        del zipimport._zip_directory_cache[docPath]

g_exportedScripts = init, cleanup
