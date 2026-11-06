using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;
using CAXA.A5.EAP.Common;
using CAXA.A5.EAP.Service;

class CaxaHelper
{
    static readonly string[] ProbeDirs =
    {
        @"C:\Program Files (x86)\Common Files\CAXA Shared\CAXA Integration\EAPInterface\1.0\EAP 2011r3",
        @"D:\Program Files (x86)\Common Files\CAXA Shared\CAXA Integration\EAPInterface\1.0\EAP 2011r3",
        @"E:\Program Files (x86)\Common Files\CAXA Shared\CAXA Integration\EAPInterface\1.0\EAP 2011r3",
        @"C:\Program Files\Common Files\CAXA Shared\CAXA Integration\EAPInterface\1.0\EAP 2011r3",
        @"D:\Program Files\Common Files\CAXA Shared\CAXA Integration\EAPInterface\1.0\EAP 2011r3",
        @"C:\Windows\Microsoft.NET\assembly\GAC_32",
        @"C:\Windows\Microsoft.NET\assembly\GAC_MSIL"
    };

    static CaxaHelper()
    {
        AppDomain.CurrentDomain.AssemblyResolve += (s, e) => Resolve(e.Name);
    }

    static Assembly Resolve(string nameText)
    {
        try
        {
            AssemblyName name = new AssemblyName(nameText);
            foreach (string dir in ProbeDirs)
            {
                string path = Path.Combine(dir, name.Name + ".dll");
                if (File.Exists(path)) return Assembly.LoadFrom(path);
                if (dir.EndsWith("GAC_32") || dir.EndsWith("GAC_MSIL"))
                {
                    string gacDir = Path.Combine(dir, name.Name);
                    if (Directory.Exists(gacDir))
                    {
                        foreach (string dll in Directory.GetFiles(gacDir, "*.dll", SearchOption.AllDirectories))
                        {
                            try
                            {
                                Assembly a = Assembly.LoadFrom(dll);
                                if (a.GetName().Name == name.Name) return a;
                            }
                            catch { }
                        }
                    }
                }
            }
        }
        catch { }
        return null;
    }

    static JavaScriptSerializer _json = new JavaScriptSerializer();
    static EAPService _svc;

    static Dictionary<string, object> Ok(object extra = null)
    {
        var d = new Dictionary<string, object> { { "ok", true } };
        if (extra != null)
        {
            foreach (var kv in (Dictionary<string, object>)extra) d[kv.Key] = kv.Value;
        }
        return d;
    }

    static Dictionary<string, object> Err(string msg)
    {
        return new Dictionary<string, object> { { "ok", false }, { "error", msg } };
    }

    static Dictionary<string, object> AttrDict(IEAPEntityBase e)
    {
        var d = new Dictionary<string, object>
        {
            { "class", e.GetType().Name },
            { "class_id", e.ClassID },
            { "object_id", e.ObjectID }
        };
        try { d["primary"] = e.GetPrimaryAttributesAsString("|"); } catch { }
        foreach (string attr in new[] { "图号", "名称", "成品代码", "整机代码", "上级图号", "物料代码", "规格型号" })
        {
            try
            {
                string v = e.GetValueAsString(attr);
                if (!string.IsNullOrEmpty(v)) d[attr] = v;
            }
            catch { }
        }
        IEAPProductEntity p = e as IEAPProductEntity;
        if (p != null)
        {
            d["identity"] = p.IdentityNo;
            d["name"] = p.Name;
        }
        else
        {
            IEAPPartEntity pt = e as IEAPPartEntity;
            if (pt != null)
            {
                d["identity"] = pt.IdentityNo;
                d["name"] = pt.Name;
                d["is_borrowed"] = pt.IsBorrowed;
            }
            else
            {
                IEAPDrawingEntity dr = e as IEAPDrawingEntity;
                if (dr != null)
                {
                    d["identity"] = dr.DrawNo;
                    d["name"] = dr.Name;
                    d["file_name"] = dr.FileName;
                    d["file_type"] = dr.FileType;
                }
                else
                {
                    IEAPFolderEntity f = e as IEAPFolderEntity;
                    if (f != null)
                    {
                        d["name"] = f.Name;
                        d["description"] = f.Description;
                    }
                    else
                    {
                        IEAPProductFolderEntity pf = e as IEAPProductFolderEntity;
                        if (pf != null)
                        {
                            d["identity"] = pf.IdentityNo;
                            d["name"] = pf.Name;
                        }
                    }
                }
            }
        }
        return d;
    }

    static Dictionary<string, object> DumpFolder(IEAPFolderEntity f, int depth, int maxDepth, HashSet<string> visited)
    {
        var d = AttrDict(f);
        d["kind"] = "folder";
        if (depth >= maxDepth) return d;
        string key = "F" + f.ClassID + ":" + f.ObjectID;
        if (!visited.Add(key)) { d["loop"] = true; return d; }
        var children = new List<object>();
        try
        {
            foreach (var sub in f.Children ?? new IEAPFolderEntity[0]) children.Add(DumpFolder(sub, depth + 1, maxDepth, visited));
        }
        catch { }
        try
        {
            foreach (var dr in f.GetAllMaterDrawing() ?? new IEAPDrawingEntity[0]) children.Add(DumpDrawing(dr, depth + 1, maxDepth));
        }
        catch { }
        d["children"] = children;
        return d;
    }

    static Dictionary<string, object> DumpDrawing(IEAPDrawingEntity dr, int depth, int maxDepth)
    {
        var d = AttrDict(dr);
        d["kind"] = "drawing";
        return d;
    }

    static Dictionary<string, object> DumpPart(IEAPPartEntity pt, int depth, int maxDepth, HashSet<string> visited)
    {
        var d = AttrDict(pt);
        d["kind"] = "part";
        if (depth >= maxDepth) return d;
        string key = "P" + pt.ClassID + ":" + pt.ObjectID;
        if (!visited.Add(key)) { d["loop"] = true; return d; }
        var children = new List<object>();
        try
        {
            foreach (var sub in pt.Children(null) ?? new IEAPPart2PartEntity[0])
            {
                IEAPPartEntity sp = null;
                try { sp = sub.RefSubPart; } catch { }
                if (sp != null) children.Add(DumpPart(sp, depth + 1, maxDepth, visited));
            }
        }
        catch { }
        try
        {
            foreach (var comp in pt.GetAllMaterDrawing() ?? new CompositeEntity[0])
            {
                IEAPDrawingEntity de = comp.Entity as IEAPDrawingEntity;
                if (de != null) children.Add(DumpDrawing(de, depth + 1, maxDepth));
            }
        }
        catch { }
        d["children"] = children;
        return d;
    }

    static Dictionary<string, object> DumpProduct(IEAPProductEntity p, int depth, int maxDepth, HashSet<string> visited)
    {
        var d = AttrDict(p);
        d["kind"] = "product";
        if (depth >= maxDepth) return d;
        string key = "PR" + p.ClassID + ":" + p.ObjectID;
        if (!visited.Add(key)) { d["loop"] = true; return d; }
        var children = new List<object>();
        try
        {
            foreach (var f in p.RelatedFolders() ?? new IEAPFolderEntity[0]) children.Add(DumpFolder(f, depth + 1, maxDepth, visited));
        }
        catch { }
        try
        {
            foreach (var pf in p.RelatedProductFolders() ?? new IEAPProductFolderEntity[0])
            {
                var pd = AttrDict(pf);
                pd["kind"] = "product_folder";
                children.Add(pd);
            }
        }
        catch { }
        try
        {
            foreach (var dr in p.RelatedDrawings() ?? new IEAPDrawingEntity[0]) children.Add(DumpDrawing(dr, depth + 1, maxDepth));
        }
        catch { }
        try
        {
            foreach (var link in p.RelatedRootParts((IEAPPrd2PartEntity)null) ?? new IEAPPrd2PartEntity[0])
            {
                IEAPPartEntity pt = null;
                try { pt = link.RefPart; } catch { }
                if (pt != null)
                {
                    var pd = DumpPart(pt, depth + 1, maxDepth, visited);
                    try { pd["link_reference"] = link.Reference; } catch { }
                    children.Add(pd);
                }
            }
        }
        catch { }
        d["children"] = children;
        return d;
    }

    static IEAPFolderEntity FindFolderById(IEAPFolderEntity root, int id, HashSet<string> visited)
    {
        if (root == null) return null;
        if (root.ObjectID == id) return root;
        string key = "F" + root.ClassID + ":" + root.ObjectID;
        if (!visited.Add(key)) return null;
        try
        {
            foreach (var sub in root.Children ?? new IEAPFolderEntity[0])
            {
                IEAPFolderEntity f = FindFolderById(sub, id, visited);
                if (f != null) return f;
            }
        }
        catch { }
        return null;
    }

    static IEAPFolderEntity FindFolderById(int id)
    {
        foreach (var root in _svc.GetAllRootFolders() ?? new IEAPFolderEntity[0])
        {
            IEAPFolderEntity f = FindFolderById(root, id, new HashSet<string>());
            if (f != null) return f;
        }
        return null;
    }

    static IEAPDrawingEntity FindDrawingInFolder(IEAPFolderEntity f, int id, HashSet<string> visited)
    {
        if (f == null) return null;
        string key = "F" + f.ClassID + ":" + f.ObjectID;
        if (!visited.Add(key)) return null;
        try
        {
            foreach (var de in f.GetAllMaterDrawing() ?? new IEAPDrawingEntity[0])
                if (de != null && de.ObjectID == id) return de;
            foreach (var sub in f.Children ?? new IEAPFolderEntity[0])
            {
                IEAPDrawingEntity de = FindDrawingInFolder(sub, id, visited);
                if (de != null) return de;
            }
        }
        catch { }
        return null;
    }

    static IEAPDrawingEntity FindDrawingById(int id)
    {
        foreach (var prod in _svc.GetAllRootProducts() ?? new IEAPProductEntity[0])
        {
            try
            {
                foreach (var dr in prod.RelatedDrawings() ?? new IEAPDrawingEntity[0])
                    if (dr.ObjectID == id) return dr;
                foreach (var f in prod.RelatedFolders() ?? new IEAPFolderEntity[0])
                {
                    IEAPDrawingEntity de = FindDrawingInFolder(f, id, new HashSet<string>());
                    if (de != null) return de;
                }
            }
            catch { }
        }
        foreach (var f in _svc.GetAllRootFolders() ?? new IEAPFolderEntity[0])
        {
            IEAPDrawingEntity de = FindDrawingInFolder(f, id, new HashSet<string>());
            if (de != null) return de;
        }
        return null;
    }

    static Dictionary<string, object> CmdLogin(Dictionary<string, object> a)
    {
        string ip = (string)a["server_ip"];
        int port = Convert.ToInt32(a["server_port"]);
        string user = (string)a["user"];
        string pass = (string)a["password"];
        if (_svc == null) _svc = new EAPService();
        // 新进程/换账号都要重新登录；旧会话先结束，避免残留身份。
        try
        {
            if (_svc.HasLogin) _svc.Logout();
        }
        catch { }
        _svc.ApplicationName = "CAXAVAULT";
        bool ok = false;
        try
        {
            ok = _svc.Login(ref user, ref pass, ip, port);
        }
        catch (Exception ex)
        {
            var failed = Err("login failed: " + ex.Message);
            failed["stage"] = "login";
            failed["detail"] = ex.ToString();
            failed["logged_in"] = false;
            return failed;
        }
        if (!ok || !_svc.HasLogin)
        {
            var failed = Err("login returned false");
            failed["stage"] = "login";
            failed["logged_in"] = false;
            return failed;
        }
        try
        {
            var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
            var session = engine != null ? engine.CurSession : null;
            if (session != null) session.IsSilentMode = true;
        }
        catch { }
        try { _svc.SetWindowVisible(false); } catch { }
        return Ok(new Dictionary<string, object>
        {
            { "version", _svc.Version },
            { "user", user },
            { "logged_in", true },
            { "server_ip", ip },
            { "server_port", port }
        });
    }

    static Dictionary<string, object> CmdLogout()
    {
        if (_svc != null && _svc.HasLogin) _svc.Logout();
        return Ok();
    }

    static Dictionary<string, object> CmdDumpTree(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int maxDepth = a.ContainsKey("max_depth") ? Convert.ToInt32(a["max_depth"]) : 4;
        var visited = new HashSet<string>();
        var roots = new List<object>();
        foreach (var p in _svc.GetAllRootProducts() ?? new IEAPProductEntity[0]) roots.Add(DumpProduct(p, 0, maxDepth, visited));
        foreach (var pf in _svc.GetAllRootProductFolders() ?? new IEAPProductFolderEntity[0])
        {
            var pd = AttrDict(pf);
            pd["kind"] = "product_folder";
            roots.Add(pd);
        }
        foreach (var f in _svc.GetAllRootFolders() ?? new IEAPFolderEntity[0]) roots.Add(DumpFolder(f, 0, maxDepth, visited));
        foreach (var dr in _svc.GetAllRootDrawings() ?? new IEAPDrawingEntity[0]) roots.Add(DumpDrawing(dr, 0, maxDepth));
        return Ok(new Dictionary<string, object> { { "roots", roots }, { "user", _svc.CurSysUser != null ? _svc.CurSysUser.UserName : null } });
    }

    static Dictionary<string, object> CmdFind(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        string identity = Convert.ToString(a["identity"]);
        string kind = a.ContainsKey("kind") ? Convert.ToString(a["kind"]) : "all";
        var found = new List<object>();
        var visited = new HashSet<string>();
        if (kind == "product" || kind == "all")
        {
            foreach (var p in _svc.GetAllRootProducts() ?? new IEAPProductEntity[0])
            {
                if (string.Equals(p.IdentityNo, identity, StringComparison.OrdinalIgnoreCase)) found.Add(AttrDict(p));
            }
        }
        return Ok(new Dictionary<string, object> { { "found", found } });
    }

    static Dictionary<string, object> CmdListProducts()
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        var list = new List<object>();
        var seen = new HashSet<int>();
        Action<IEAPProductEntity> addProduct = p =>
        {
            if (seen.Add(p.ObjectID))
            {
                list.Add(new Dictionary<string, object>
                {
                    { "object_id", p.ObjectID },
                    { "class_id", p.ClassID },
                    { "identity", p.IdentityNo },
                    { "name", p.Name }
                });
            }
        };
        foreach (var p in _svc.GetAllRootProducts() ?? new IEAPProductEntity[0])
            addProduct(p);
        foreach (var pf in _svc.GetAllRootProductFolders() ?? new IEAPProductFolderEntity[0])
            CollectProducts(pf, addProduct);
        return Ok(new Dictionary<string, object> { { "products", list } });
    }

    static void CollectProducts(IEAPProductFolderEntity pf, Action<IEAPProductEntity> add)
    {
        if (pf == null) return;
        try
        {
            foreach (var prod in pf.RelatedProducts ?? new IEAPProductEntity[0]) add(prod);
            foreach (var sub in pf.Children ?? new IEAPProductFolderEntity[0]) CollectProducts(sub, add);
        }
        catch { }
    }

    static IEAPProductEntity FindProductRecursive(string keyword, out string path)
    {
        path = null;
        foreach (var p in _svc.GetAllRootProducts() ?? new IEAPProductEntity[0])
        {
            if (ProductMatches(p, keyword)) { path = "根"; return p; }
        }
        foreach (var pf in _svc.GetAllRootProductFolders() ?? new IEAPProductFolderEntity[0])
        {
            IEAPProductEntity found = null;
            string cur = FindInProductFolder(pf, keyword, out found);
            if (found != null) { path = cur; return found; }
        }
        return null;
    }

    static bool ProductMatches(IEAPProductEntity p, string keyword)
    {
        string kw = keyword.Trim();
        bool idOk = !string.IsNullOrEmpty(p.IdentityNo) &&
                    p.IdentityNo.IndexOf(kw, StringComparison.OrdinalIgnoreCase) >= 0;
        bool nameOk = !string.IsNullOrEmpty(p.Name) &&
                      p.Name.IndexOf(kw, StringComparison.OrdinalIgnoreCase) >= 0;
        return idOk || nameOk;
    }

    static string FindInProductFolder(IEAPProductFolderEntity pf, string keyword, out IEAPProductEntity found)
    {
        found = null;
        if (pf == null) return "";
        try
        {
            foreach (var prod in pf.RelatedProducts ?? new IEAPProductEntity[0])
            {
                if (ProductMatches(prod, keyword)) { found = prod; return pf.Name; }
            }
            foreach (var sub in pf.Children ?? new IEAPProductFolderEntity[0])
            {
                string subPath = FindInProductFolder(sub, keyword, out found);
                if (found != null) return (string.IsNullOrEmpty(pf.Name) ? "" : pf.Name + " / ") + subPath;
            }
        }
        catch { }
        return "";
    }

    static List<IEAPProductEntity> GetAllProductsList()
    {
        var all = new List<IEAPProductEntity>();
        var seen = new HashSet<int>();
        Action<IEAPProductEntity> add = p =>
        {
            if (seen.Add(p.ObjectID)) all.Add(p);
        };
        foreach (var p in _svc.GetAllRootProducts() ?? new IEAPProductEntity[0]) add(p);
        foreach (var pf in _svc.GetAllRootProductFolders() ?? new IEAPProductFolderEntity[0]) CollectProducts(pf, add);
        return all;
    }

    static bool PartMatches(IEAPPartEntity pt, string keyword)
    {
        string kw = keyword.Trim();
        bool idOk = !string.IsNullOrEmpty(pt.IdentityNo) &&
                    pt.IdentityNo.IndexOf(kw, StringComparison.OrdinalIgnoreCase) >= 0;
        bool nameOk = !string.IsNullOrEmpty(pt.Name) &&
                      pt.Name.IndexOf(kw, StringComparison.OrdinalIgnoreCase) >= 0;
        return idOk || nameOk;
    }

    static void SearchPartRecursive(IEAPPartEntity pt, string keyword, List<Dictionary<string, object>> found,
                                    HashSet<string> visited, string path, int limit)
    {
        if (pt == null || found.Count >= limit) return;
        string key = "P" + pt.ClassID + ":" + pt.ObjectID;
        if (!visited.Add(key)) return;
        if (PartMatches(pt, keyword))
        {
            found.Add(new Dictionary<string, object>
            {
                { "part_id", pt.ObjectID },
                { "identity", pt.IdentityNo },
                { "name", pt.Name },
                { "is_borrowed", pt.IsBorrowed },
                { "path", path }
            });
            if (found.Count >= limit) return;
        }
        try
        {
            foreach (var l in pt.Children(null) ?? new IEAPPart2PartEntity[0])
            {
                if (l == null || l.RefSubPart == null) continue;
                SearchPartRecursive(l.RefSubPart, keyword, found, visited, path + " / " + pt.IdentityNo, limit);
            }
        }
        catch { }
    }

    static Dictionary<string, object> CmdFindPart(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        string keyword = Convert.ToString(a["keyword"]);
        int limit = a.ContainsKey("limit") ? Convert.ToInt32(a["limit"]) : 5;
        var found = new List<Dictionary<string, object>>();
        int productsScanned = 0;
        foreach (var prod in GetAllProductsList())
        {
            productsScanned++;
            try
            {
                foreach (var link in prod.RelatedRootParts((IEAPPrd2PartEntity)null) ?? new IEAPPrd2PartEntity[0])
                {
                    if (link == null || link.RefPart == null) continue;
                    SearchPartRecursive(link.RefPart, keyword, found, new HashSet<string>(),
                        "产品 " + (prod.IdentityNo ?? "") + " " + (prod.Name ?? ""), limit);
                    if (found.Count >= limit) break;
                }
            }
            catch { }
            if (found.Count >= limit) break;
        }
        return Ok(new Dictionary<string, object> { { "found", found }, { "products_scanned", productsScanned } });
    }

    static Dictionary<string, object> CmdPartInfo(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int id = Convert.ToInt32(a["part_id"]);
        IEAPPartEntity pt = _svc.GetPart(id);
        if (pt == null) return Err("part not found: " + id);
        var res = new Dictionary<string, object>
        {
            { "part_id", pt.ObjectID },
            { "identity", pt.IdentityNo },
            { "name", pt.Name },
            { "is_borrowed", pt.IsBorrowed }
        };
        var children = new List<object>();
        try
        {
            foreach (var l in pt.Children(null) ?? new IEAPPart2PartEntity[0])
            {
                var d = new Dictionary<string, object>
                {
                    { "sub_identity", l.RefSubPart != null ? l.RefSubPart.IdentityNo : null },
                    { "sub_name", l.RefSubPart != null ? l.RefSubPart.Name : null },
                    { "sub_id", l.RefSubPart != null ? (object)l.RefSubPart.ObjectID : null },
                    { "reference", l.Reference },
                    { "quantity", l.AssemblyQuantity },
                    { "serial", l.SerialNo }
                };
                if (l.RefSubPart == null)
                {
                    try { l.Refresh(); } catch { }
                    try
                    {
                        d["sub_identity2"] = l.RefSubPart != null ? l.RefSubPart.IdentityNo : null;
                        d["sub_name2"] = l.RefSubPart != null ? l.RefSubPart.Name : null;
                    }
                    catch { }
                    try
                    {
                        var props = l.GetType().GetProperties();
                        foreach (var prop in props)
                        {
                            try
                            {
                                object val = prop.GetValue(l, null);
                                d["prop_" + prop.Name] = val == null ? null : val.ToString();
                            }
                            catch { }
                        }
                    }
                    catch { }
                }
                children.Add(d);
            }
            res["children_count"] = children.Count;
        }
        catch (Exception ex)
        {
            res["children_error"] = ex.Message;
        }
        res["children"] = children;
        try
        {
            var parents = new List<object>();
            foreach (var l in pt.ParentParts(null) ?? new IEAPPart2PartEntity[0])
            {
                parents.Add(new Dictionary<string, object>
                {
                    { "parent_identity", l.RefParentPart != null ? l.RefParentPart.IdentityNo : null },
                    { "parent_name", l.RefParentPart != null ? l.RefParentPart.Name : null },
                    { "parent_id", l.RefParentPart != null ? (object)l.RefParentPart.ObjectID : null },
                    { "reference", l.Reference }
                });
            }
            res["parents"] = parents;
        }
        catch (Exception ex)
        {
            res["parents_error"] = ex.Message;
        }
        try
        {
            var prods = new List<object>();
            foreach (var l in pt.ParentProducts(null) ?? new IEAPPrd2PartEntity[0])
            {
                prods.Add(new Dictionary<string, object>
                {
                    { "product_identity", l.RefProduct != null ? l.RefProduct.IdentityNo : null },
                    { "product_name", l.RefProduct != null ? l.RefProduct.Name : null },
                    { "product_id", l.RefProduct != null ? (object)l.RefProduct.ObjectID : null },
                    { "reference", l.Reference }
                });
            }
            res["products"] = prods;
        }
        catch (Exception ex)
        {
            res["products_error"] = ex.Message;
        }
        try
        {
            var linked = new List<object>();
            foreach (var ent in _svc.RetrieveAllLinksOfObject((short)1019, id) ?? new IEAPEntityBase[0])
            {
                var d = new Dictionary<string, object> { { "class", ent.GetType().Name }, { "object_id", ent.ObjectID } };
                string ident = null, nm = null;
                try { ident = ent.GetValueAsString("IdentityNo"); } catch { }
                try { nm = ent.GetValueAsString("name"); } catch { }
                if (string.IsNullOrEmpty(ident))
                {
                    IEAPPartEntity lp = ent as IEAPPartEntity;
                    if (lp != null) ident = lp.IdentityNo;
                    IEAPDrawingEntity ld = ent as IEAPDrawingEntity;
                    if (ld != null) ident = ld.DrawNo;
                }
                d["identity"] = ident;
                d["name"] = nm;
                linked.Add(d);
            }
            res["all_linked_objects"] = linked;
        }
        catch (Exception ex)
        {
            res["all_linked_error"] = ex.Message;
        }
        var linkEndpoints = new List<object>();
        try
        {
            var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
            var session = engine != null ? engine.CurSession : null;
            if (session != null)
            {
                var store = session.ObjectStore;
                foreach (var l in pt.Children(null) ?? new IEAPPart2PartEntity[0])
                {
                    var ep = new Dictionary<string, object> { { "reference", l.Reference }, { "quantity", l.AssemblyQuantity } };
                    try { ep["link_class"] = l.ClassID; } catch { }
                    try { ep["link_id"] = l.ObjectID; } catch { }
                    try
                    {
                        var lo = store.RetrieveObject((short)Convert.ToInt32(ep["link_class"]),
                                                      Convert.ToInt32(ep["link_id"]))
                                   as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxLinkObject;
                        if (lo != null)
                        {
                            AddEndpoint(ep, "A", lo.RoleAObject);
                            AddEndpoint(ep, "B", lo.RoleBObject);
                        }
                        else
                        {
                            ep["link_error"] = "not ICaxLinkObject";
                        }
                    }
                    catch (Exception ex)
                    {
                        ep["link_error"] = ex.Message;
                    }
                    linkEndpoints.Add(ep);
                }
            }
            else
            {
                res["link_endpoints_session"] = "unavailable";
            }
        }
        catch (Exception ex)
        {
            res["link_endpoints_error"] = ex.Message;
        }
        res["link_endpoints"] = linkEndpoints;
        try
        {
            var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
            var session = engine != null ? engine.CurSession : null;
            if (session == null)
            {
                res["lowlevel"] = "session unavailable";
            }
            else
            {
                var store = session.ObjectStore;
                var obj = store.RetrieveObject((short)1019, id) as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
                res["lowlevel_obj"] = obj != null ? "ok" : "null";
                if (obj == null)
                {
                    res["lowlevel"] = "obj null";
                }
                else
                {
                    res["lowlevel_direct_children"] = SafeEnumerate("direct_children", () => TryEnumerate(obj.RetrieveDirectChildren("")));
                    res["lowlevel_relations"] = SafeEnumerate("relations", () => TryEnumerate(obj.RetrieveRelations(null)));
                    res["lowlevel_roleb_relations"] = SafeEnumerate("roleb", () => TryEnumerate(obj.RetrieveRoleBRelations(null)));
                    res["lowlevel_all_links"] = SafeEnumerate("all_links", () => TryEnumerate(obj.RetrieveAllLinks()));
                }
            }
        }
        catch (Exception ex)
        {
            res["lowlevel_error"] = ex.Message;
        }
        return Ok(res);
    }

    static void AddEndpoint(Dictionary<string, object> ep, string tag,
                            CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject obj)
    {
        var d = new Dictionary<string, object>();
        if (obj == null)
        {
            ep["role" + tag] = null;
            return;
        }
        var ob = obj as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase;
        if (ob != null)
        {
            try { d["class"] = ob.ClassName; } catch { }
            try { d["object_id"] = ob.ObjectId; } catch { }
            try { d["identity"] = ob.GetValueAsString("IdentityNo"); } catch { }
            try { d["name"] = ob.GetValueAsString("name"); } catch { }
        }
        ep["role" + tag] = d;
    }

    static object SafeEnumerate(string label, Func<object> fn)
    {
        try
        {
            return fn();
        }
        catch (Exception ex)
        {
            return label + " error: " + ex.Message;
        }
    }

    static object TryEnumerate(System.Collections.IEnumerable en)
    {
        var list = new List<object>();
        try
        {
            if (en == null) return "null";
            foreach (object o in en)
            {
                if (o == null) { list.Add(null); continue; }
                var d = new Dictionary<string, object> { { "type", o.GetType().Name } };
                var ob = o as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase;
                if (ob != null)
                {
                    d["class"] = ob.ClassName;
                    d["object_id"] = ob.ObjectId;
                    try { d["identity"] = ob.GetValueAsString("IdentityNo"); } catch { }
                    try { d["name"] = ob.GetValueAsString("name"); } catch { }
                }
                list.Add(d);
            }
            return list;
        }
        catch (Exception ex)
        {
            return "error: " + ex.Message;
        }
    }

    static Dictionary<string, object> CmdFindPartInProduct(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int productId = Convert.ToInt32(a["product_id"]);
        string keyword = Convert.ToString(a["keyword"]);
        int limit = a.ContainsKey("limit") ? Convert.ToInt32(a["limit"]) : 20;
        IEAPProductEntity prod = _svc.GetProduct(productId);
        if (prod == null) return Err("product not found: " + productId);
        var found = new List<Dictionary<string, object>>();
        try
        {
            foreach (var link in prod.RelatedRootParts((IEAPPrd2PartEntity)null) ?? new IEAPPrd2PartEntity[0])
            {
                if (link == null || link.RefPart == null) continue;
                SearchPartRecursive(link.RefPart, keyword, found, new HashSet<string>(),
                    "产品 " + (prod.IdentityNo ?? "") + " " + (prod.Name ?? ""), limit);
                if (found.Count >= limit) break;
            }
        }
        catch { }
        return Ok(new Dictionary<string, object> { { "found", found } });
    }

    static Dictionary<string, object> CmdQueryParts(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        string keyword = Convert.ToString(a["keyword"]);
        var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
        var session = engine != null ? engine.CurSession : null;
        if (session == null) return Err("session unavailable");
        try
        {
            var store = session.ObjectStore;
            var q = store.NewQuery();
            var cond = q.QueryCondition;
            cond.QueriedClassID = 1019;
            var where = store.NewQueryWhere();
            where.PropertyName = "IdentityNo";
            where.OperatorType = CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.EnumWhereOperatorTypes.OP_Contain;
            where.OperatorValue = keyword;
            cond.AddWhere(where);
            bool ok = q.Run();
            var qr = q.QueryResult;
            var items = TryEnumerate(qr.GetAllObjectBases() as System.Collections.IEnumerable);
            return Ok(new Dictionary<string, object> { { "run", ok }, { "count", qr.Count }, { "items", items } });
        }
        catch (Exception ex)
        {
            return Err("query error: " + ex.Message);
        }
    }

    // Read-only generic object query for diagnostics and metadata inspection.
    static Dictionary<string, object> CmdQueryObjects(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        short classId = Convert.ToInt16(a["class_id"]);
        int limit = a.ContainsKey("limit") ? Convert.ToInt32(a["limit"]) : 100;
        var propertyNames = new List<string>();
        if (a.ContainsKey("properties") && a["properties"] != null)
        {
            var values = a["properties"] as System.Collections.IEnumerable;
            if (values != null)
            {
                foreach (object value in values)
                {
                    if (value != null) propertyNames.Add(Convert.ToString(value));
                }
            }
        }
        var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
        var session = engine != null ? engine.CurSession : null;
        if (session == null) return Err("session unavailable");
        try
        {
            var store = session.ObjectStore;
            var query = store.NewQuery();
            var condition = query.QueryCondition;
            condition.QueriedClassID = classId;
            if (a.ContainsKey("where_property") && a["where_property"] != null &&
                a.ContainsKey("where_value") && a["where_value"] != null)
            {
                var where = store.NewQueryWhere();
                where.PropertyName = Convert.ToString(a["where_property"]);
                string op = a.ContainsKey("where_operator")
                    ? Convert.ToString(a["where_operator"]).ToLowerInvariant()
                    : "contain";
                switch (op)
                {
                    case "equal":
                        where.OperatorType = CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.EnumWhereOperatorTypes.OP_Equal;
                        break;
                    case "not_equal":
                        where.OperatorType = CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.EnumWhereOperatorTypes.OP_NotEqual;
                        break;
                    case "prefix":
                        where.OperatorType = CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.EnumWhereOperatorTypes.OP_PreContain;
                        break;
                    case "suffix":
                        where.OperatorType = CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.EnumWhereOperatorTypes.OP_PostContain;
                        break;
                    default:
                        where.OperatorType = CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.EnumWhereOperatorTypes.OP_Contain;
                        break;
                }
                where.OperatorValue = Convert.ToString(a["where_value"]);
                condition.AddWhere(where);
            }
            bool run = query.Run();
            var items = new List<object>();
            foreach (object raw in query.QueryResult.GetAllObjectBases() as System.Collections.IEnumerable)
            {
                var obj = raw as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase;
                if (obj == null) continue;
                var item = new Dictionary<string, object>
                {
                    { "object_id", obj.ObjectId },
                    { "class_name", obj.ClassName }
                };
                foreach (string propertyName in propertyNames)
                {
                    try
                    {
                        item[propertyName] = obj.GetValueAsString(propertyName);
                    }
                    catch (Exception ex)
                    {
                        item[propertyName] = "ERROR: " + ex.Message;
                    }
                }
                items.Add(item);
                if (items.Count >= limit) break;
            }
            return Ok(new Dictionary<string, object>
            {
                { "run", run },
                { "count", query.QueryResult.Count },
                { "items", items }
            });
        }
        catch (Exception ex)
        {
            return Err("query_objects error: " + ex.Message);
        }
    }

    static Dictionary<string, object> CmdQueryLinks(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        short classId = Convert.ToInt16(a["class_id"]);
        int limit = a.ContainsKey("limit") ? Convert.ToInt32(a["limit"]) : 100;
        var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
        var session = engine != null ? engine.CurSession : null;
        if (session == null) return Err("session unavailable");
        try
        {
            var store = session.ObjectStore;
            var query = store.NewQuery();
            query.QueryCondition.QueriedClassID = classId;
            bool run = query.Run();
            var items = new List<object>();
            foreach (object raw in query.QueryResult.GetAllObjectBases() as System.Collections.IEnumerable)
            {
                var link = raw as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxLinkObject;
                if (link == null) continue;
                var item = new Dictionary<string, object>
                {
                    { "link_class_id", link.RefLinkClass != null ? link.RefLinkClass.ClassId : 0 },
                    { "link_object_id", link.ObjectId }
                };
                foreach (var pair in new[]
                {
                    new { Name = "roleA", Obj = link.RoleAObject },
                    new { Name = "roleB", Obj = link.RoleBObject }
                })
                {
                    if (pair.Obj == null)
                    {
                        item[pair.Name] = null;
                        continue;
                    }
                    var endpoint = new Dictionary<string, object>
                    {
                        { "class_name", pair.Obj.ClassName },
                        { "object_id", pair.Obj.ObjectId }
                    };
                    try { endpoint["identity"] = pair.Obj.GetValueAsString("IdentityNo"); } catch { }
                    try { endpoint["name"] = pair.Obj.GetValueAsString("name"); } catch { }
                    item[pair.Name] = endpoint;
                }
                items.Add(item);
                if (items.Count >= limit) break;
            }
            return Ok(new Dictionary<string, object>
            {
                { "run", run },
                { "count", query.QueryResult.Count },
                { "items", items }
            });
        }
        catch (Exception ex)
        {
            return Err("query_links error: " + ex.Message);
        }
    }

    static Dictionary<string, object> CmdWorkflowDalProbe(Dictionary<string, object> a)
    {
        var result = new Dictionary<string, object>();
        try
        {
            var process = new Interop.EAPLib.CxDalWfProcessClass();
            result["process_type"] = process.GetType().FullName;
            result["process_connection_id"] = process.connection_id;
            result["process_server_type"] = process.server_type;
            result["process_retry_connection_number"] = process.retry_connection_number;
            try
            {
                result["process_server_info"] = process.server_info != null
                    ? process.server_info.GetType().FullName
                    : null;
            }
            catch (Exception ex) { result["process_server_info_error"] = ex.Message; }
        }
        catch (Exception ex)
        {
            result["process_error"] = ex.ToString();
        }
        try
        {
            var connection = new Interop.EAPLib.CxDalWfConnectionClass();
            result["connection_type"] = connection.GetType().FullName;
            result["connection_connection_id"] = connection.connection_id;
            result["connection_server_type"] = connection.server_type;
            result["connection_retry_connection_number"] = connection.retry_connection_number;
        }
        catch (Exception ex)
        {
            result["connection_error"] = ex.ToString();
        }
        return Ok(result);
    }

    static Dictionary<string, object> CmdWorkflowConnectionProbe(Dictionary<string, object> a)
    {
        if (!a.ContainsKey("server_ip") || !a.ContainsKey("server_port") ||
            !a.ContainsKey("user_id"))
            return Err("server_ip, server_port, user_id are required");
        var result = new Dictionary<string, object>();
        Interop.EAPLib.CxDalWfConnectionClass connection = null;
        try
        {
            var serverInfo = new Interop.EAPLib.CxServerInfoClass
            {
                server_ip = Convert.ToString(a["server_ip"]),
                server_port = Convert.ToUInt32(a["server_port"]),
                server_name = a.ContainsKey("server_name")
                    ? Convert.ToString(a["server_name"])
                    : "Workflow"
            };
            connection = new Interop.EAPLib.CxDalWfConnectionClass();
            connection.server_type = (int)Interop.EAPLib.PLMSERVERTYPE.PLM_SERVERTYPE_WORKFLOWSERVER;
            connection.server_info = serverInfo;
            uint connectionId = connection.connect(Convert.ToUInt32(a["user_id"]));
            connection.connection_id = connectionId;
            result["server_ip"] = serverInfo.server_ip;
            result["server_port"] = serverInfo.server_port;
            result["connection_id"] = connectionId;
            result["server_type"] = connection.server_type;
            return Ok(result);
        }
        catch (Exception ex)
        {
            result["error"] = ex.ToString();
            return Ok(result);
        }
        finally
        {
            if (connection != null)
            {
                try
                {
                    if (connection.connection_id != 0)
                        connection.disconnect(connection.connection_id);
                }
                catch { }
            }
        }
    }

    static Dictionary<string, object> CmdRelatedObjects(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        short classId = Convert.ToInt16(a["class_id"]);
        int objectId = Convert.ToInt32(a["object_id"]);
        string roleName = a.ContainsKey("role_name")
            ? Convert.ToString(a["role_name"])
            : "";
        var propertyNames = new List<string>();
        if (a.ContainsKey("properties") && a["properties"] != null)
        {
            var values = a["properties"] as System.Collections.IEnumerable;
            if (values != null)
            {
                foreach (object value in values)
                    if (value != null) propertyNames.Add(Convert.ToString(value));
            }
        }
        var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
        var session = engine != null ? engine.CurSession : null;
        if (session == null) return Err("session unavailable");
        try
        {
            var store = session.ObjectStore;
            var obj = store.RetrieveObject(classId, objectId)
                as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
            if (obj == null) return Err("source object not found");
            var items = new List<object>();
            var related = obj.RetrieveDirectChildren(roleName) as System.Collections.IEnumerable;
            if (related != null)
            {
                foreach (object raw in related)
                {
                    var relObj = raw as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase;
                    if (relObj == null) continue;
                    var item = new Dictionary<string, object>
                    {
                        { "class_name", relObj.ClassName },
                        { "object_id", relObj.ObjectId }
                    };
                    foreach (string propertyName in propertyNames)
                    {
                        try { item[propertyName] = relObj.GetValueAsString(propertyName); }
                        catch (Exception ex) { item[propertyName] = "ERROR: " + ex.Message; }
                    }
                    items.Add(item);
                }
            }
            return Ok(new Dictionary<string, object>
            {
                { "source_class", obj.ClassName },
                { "source_id", obj.ObjectId },
                { "role_name", roleName },
                { "items", items }
            });
        }
        catch (Exception ex)
        {
            return Err("related_objects error: " + ex.ToString());
        }
    }

    static string GetClassNameByID(CAXA.A5.EAP.CommonLibrary.Session.ICaxSession session, short classId)
    {
        try
        {
            var cls = session.MetaInfo.GetClassByID(classId);
            return cls != null ? cls.ClassName : null;
        }
        catch
        {
            return null;
        }
    }

    static Dictionary<string, object> CmdClassInfo(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
        var session = engine != null ? engine.CurSession : null;
        if (session == null) return Err("session unavailable");
        var ids = new List<short>();
        if (a.ContainsKey("class_ids") && a["class_ids"] != null)
        {
            var arr = a["class_ids"] as System.Collections.IEnumerable;
            if (arr != null)
            {
                foreach (object o in arr) ids.Add(Convert.ToInt16(o));
            }
        }
        if (ids.Count == 0) ids.AddRange(new short[] { 1002, 1003, 1001 });
        var list = new List<object>();
        foreach (var id in ids)
        {
            try
            {
                var cls = session.MetaInfo.GetClassByID(id);
                list.Add(new Dictionary<string, object>
                {
                    { "id", id },
                    { "name", cls != null ? cls.ClassName : null },
                    { "ext", cls != null ? cls.ExternalName : null }
                });
            }
            catch (Exception ex)
            {
                list.Add(new Dictionary<string, object> { { "id", id }, { "err", ex.Message } });
            }
        }
        return Ok(new Dictionary<string, object> { { "classes", list } });
    }

    static Dictionary<string, object> CmdDumpProduct(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        string identity = Convert.ToString(a["identity"]);
        int maxDepth = a.ContainsKey("max_depth") ? Convert.ToInt32(a["max_depth"]) : 6;
        string path;
        IEAPProductEntity target = FindProductRecursive(identity, out path);
        if (target != null)
        {
            var visited = new HashSet<string>();
            return Ok(new Dictionary<string, object> { { "product", DumpProduct(target, 0, maxDepth, visited) }, { "path", path } });
        }
        return Err("product not found: " + identity);
    }

    static Dictionary<string, object> CmdFindProduct(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        string keyword = Convert.ToString(a["keyword"]);
        int limit = a.ContainsKey("limit") ? Convert.ToInt32(a["limit"]) : 20;
        var list = new List<object>();
        foreach (var p in GetAllProductsList())
        {
            if (!ProductMatches(p, keyword)) continue;
            list.Add(new Dictionary<string, object>
            {
                { "object_id", p.ObjectID },
                { "identity", p.IdentityNo },
                { "name", p.Name }
            });
            if (list.Count >= limit) break;
        }
        return Ok(new Dictionary<string, object> { { "found", list } });
    }

    static Dictionary<string, object> CmdProductRootPart(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int productId = Convert.ToInt32(a["product_id"]);
        string identity = Convert.ToString(a["identity"]);
        IEAPProductEntity prod = _svc.GetProduct(productId);
        if (prod == null) return Err("product not found");
        try
        {
            foreach (var link in prod.RelatedRootParts((IEAPPrd2PartEntity)null) ?? new IEAPPrd2PartEntity[0])
            {
                if (link == null || link.RefPart == null) continue;
                if (string.Equals(link.RefPart.IdentityNo, identity, StringComparison.OrdinalIgnoreCase))
                {
                    return Ok(new Dictionary<string, object>
                    {
                        { "part_id", link.RefPart.ObjectID },
                        { "identity", link.RefPart.IdentityNo },
                        { "name", link.RefPart.Name }
                    });
                }
            }
        }
        catch (Exception ex)
        {
            return Err("product root part error: " + ex.Message);
        }
        return Err("part not found under product");
    }

    static Dictionary<string, object> CmdCreatePart(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        string identity = Convert.ToString(a["identity_no"]);
        string name = a.ContainsKey("name") ? Convert.ToString(a["name"]) : "";
        IEAPPartEntity pt = _svc.NewPart(identity);
        if (pt == null) return Err("NewPart returned null");
        if (!string.IsNullOrEmpty(name)) { try { pt.Name = name; } catch { } }
        pt.Insert();
        return Ok(new Dictionary<string, object> { { "object_id", pt.ObjectID }, { "identity", pt.IdentityNo } });
    }

    static Dictionary<string, object> CmdReference(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int target = Convert.ToInt32(a["target_id"]);
        int source = Convert.ToInt32(a["source_id"]);
        string kind = Convert.ToString(a["kind"]);
        if (kind == "part_to_product")
        {
            IEAPProductEntity prod = _svc.GetProduct(target);
            IEAPPartEntity part = _svc.GetPart(source);
            if (prod == null || part == null) return Err("product or part not found");
            bool ok = prod.ReferenceToMe(part);
            return ok ? Ok() : Err("ReferenceToMe returned false");
        }
        return Err("unsupported reference kind: " + kind);
    }

    static short ClassIdByName(string name)
    {
        string n = (name ?? "").Trim().ToLowerInvariant();
        if (n == "drawing") return 1002;
        if (n == "cappdocument") return 1003;
        if (n == "document") return 1001;
        return 0;
    }

    // 上传期间只允许本 helper 进程自己的导入对话框被自动确认。
    static volatile bool _uploadInProgress = false;
    static volatile int _uploadCurrentObjectId = 0;

    static Dictionary<string, object> CmdUpload(Dictionary<string, object> a)
    {
        _uploadInProgress = true;
        try
        {
            return CmdUploadCore(a);
        }
        catch (Exception ex)
        {
            // 无法证明没有写入：返回结构化“部分/待核实”，上层不得盲目重放。
            var failed = Err("upload failed: " + ex.Message);
            failed["stage"] = "upload_unhandled";
            failed["detail"] = ex.ToString();
            failed["partial"] = true;
            failed["uncertain"] = true;
            if (_uploadCurrentObjectId != 0) failed["object_id"] = _uploadCurrentObjectId;
            return failed;
        }
        finally
        {
            _uploadInProgress = false;
        }
    }

    static Dictionary<string, object> CmdUploadCore(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        string parentKind = Convert.ToString(a["parent_kind"]); // product | part | folder
        int parentId = Convert.ToInt32(a["parent_id"]);
        int drawingType = a.ContainsKey("drawing_type") ? Convert.ToInt32(a["drawing_type"]) : (int)DrawingType.Document;
        string filePath = Convert.ToString(a["file_path"]);
        string drawNo = a.ContainsKey("draw_no") ? Convert.ToString(a["draw_no"]) : "";
        string name = a.ContainsKey("name") ? Convert.ToString(a["name"]) : "";
        string onDuplicate = a.ContainsKey("on_duplicate") ? Convert.ToString(a["on_duplicate"]) : "reuse";
        if (!File.Exists(filePath)) return Err("file not found: " + filePath);
        IEAPDrawingEntity dr = null;
        if (parentKind == "product")
        {
            IEAPProductEntity prod = _svc.GetProduct(parentId);
            if (prod == null) return Err("product not found");
            dr = prod.AttachFile((DrawingType)drawingType, filePath);
        }
        else if (parentKind == "folder")
        {
            IEAPFolderEntity folder = FindFolderById(parentId);
            if (folder == null) return Err("folder not found");
            dr = folder.AttachFile((DrawingType)drawingType, filePath, false);
        }
        else if (parentKind == "part")
        {
            IEAPPartEntity part = _svc.GetPart(parentId);
            if (part == null) return Err("part not found");
            var diag = new Dictionary<string, object>();
            CAXA.A5.EAP.CommonLibrary.Session.ICaxSession sess = null;
            try
            {
                var eng = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
                sess = eng != null ? eng.CurSession : null;
            }
            catch { }
            string attachMethod = null;
            IEAPDrawingEntity existingDuplicate = null;
            // 防重：零件下已有同名文档则复用（或按配置新增版本），不再重复挂接；
            // on_duplicate == "new_version" 时也按图号（DrawNo）命中，名称被识别变体也能命中。
            // append_iteration 则故意继续走导入，让 CAXA 的重复对象对话框出现，
            // 由后台自动确认线程选择“在现有版本上追加新的版次”。
            try
            {
                foreach (var comp in part.GetAllMaterDrawing() ?? new CompositeEntity[0])
                {
                    IEAPDrawingEntity de = comp.Entity as IEAPDrawingEntity;
                    if (de == null) continue;
                    bool nameHit = !string.IsNullOrEmpty(name) &&
                        string.Equals(de.Name, name, StringComparison.OrdinalIgnoreCase);
                    bool drawNoHit = onDuplicate == "new_version" && !string.IsNullOrEmpty(drawNo) &&
                        string.Equals(de.DrawNo, drawNo, StringComparison.OrdinalIgnoreCase);
                    if (nameHit || drawNoHit)
                    {
                        diag["exists"] = de.ObjectID;
                        try { diag["class_name"] = GetClassNameByID(sess, de.ClassID); } catch { }
                        diag["match"] = nameHit ? "name" : "draw_no";
                        if (onDuplicate == "append_iteration")
                        {
                            string existingState = "";
                            try { existingState = de.StateExt ?? ""; } catch { }
                            diag["existing_state"] = existingState;
                            if (existingState.Contains("发布"))
                            {
                                return Err("失败：图纸已发布，无法追加版次（" + (de.Name ?? name) + "）");
                            }
                            existingDuplicate = de;
                            _uploadCurrentObjectId = de.ObjectID;
                            try { diag["before_iterator_no"] = de.IteratorNo; } catch { }
                            try { diag["before_history_count"] = (de.GetHistoryDrawings() ?? new IEAPDrawingIteratorEntity[0]).Length; } catch { }
                            diag["append_iteration_requested"] = true;
                            continue;
                        }
                        dr = de;
                        _uploadCurrentObjectId = de.ObjectID;
                        break;
                    }
                }
            }
            catch { }
            if (dr != null)
            {
                if (onDuplicate == "new_version")
                {
                    var nv = AddNewVersion(dr, filePath, diag);
                    if (nv != null)
                    {
                        return Ok(new Dictionary<string, object>
                        {
                            { "object_id", dr.ObjectID },
                            { "name", dr.Name },
                            { "file_name", dr.FileName },
                            { "draw_no", dr.DrawNo },
                            { "iterator_no", diag.ContainsKey("iterator_no") ? diag["iterator_no"] : null },
                            { "class_id", dr.ClassID }, { "class_name", GetClassNameByID(sess, dr.ClassID) },
                            { "attached", true },
                            { "new_version", true },
                            { "method", "new_version" },
                            { "diag", diag }
                        });
                    }
                    if (diag.ContainsKey("new_version_copy_failed"))
                    {
                        var failed = Err("新增版次失败：未能覆盖检出文件，已放弃检入（" + (dr.Name ?? "") + "）");
                        failed["stage"] = "new_version";
                        failed["object_id"] = dr.ObjectID;
                        failed["partial"] = true;
                        failed["detail"] = "copy_failed";
                        failed["diag"] = diag;
                        return failed;
                    }
                    diag["new_version_fallback"] = "reuse";
                }
                return Ok(new Dictionary<string, object>
                {
                    { "object_id", dr.ObjectID },
                    { "name", dr.Name },
                    { "file_name", dr.FileName },
                    { "class_name", diag.ContainsKey("class_name") ? diag["class_name"] : null },
                    { "attached", true },
                    { "reused", true },
                    { "method", "reuse" },
                    { "diag", diag }
                });
            }
            try { diag["allow_2d"] = part.AllowNew2DDrawing(); } catch { diag["allow_2d"] = "err"; }
            try { diag["allow_capp"] = part.AllowNewCAPPDrawing(); } catch { diag["allow_capp"] = "err"; }
            try { diag["has_children"] = (part.Children(null) ?? new IEAPPart2PartEntity[0]).Length; } catch { }
            try { diag["drawing_count"] = (part.GetAllMaterDrawing() ?? new CompositeEntity[0]).Length; } catch { }
            if (drawingType != (int)DrawingType.DrawingCAPP)
            {
                try
                {
                    dr = part.AttachFile((DrawingType)drawingType, filePath, false, false);
                    diag["attach"] = dr != null ? "ok" : "null";
                    if (dr != null) { attachMethod = "attach"; _uploadCurrentObjectId = dr.ObjectID; }
                }
                catch (Exception ex)
                {
                    diag["attach_err"] = ex.Message;
                }
                if (dr == null)
                {
                    try
                    {
                        IEAPDrawingEntity nd = null;
                        IEAPPart2PartEntity link = null;
                        part.AttachFileAsChild((DrawingType)drawingType, filePath, false, false, out nd, out link);
                        dr = nd;
                        diag["attach_as_child"] = dr != null ? "ok" : "null";
                        if (dr != null) { attachMethod = "attach_as_child"; _uploadCurrentObjectId = dr.ObjectID; }
                    }
                    catch (Exception ex)
                    {
                        diag["attach_as_child_err"] = ex.Message;
                    }
                }
                if (dr == null && drawingType != (int)DrawingType.Document)
                {
                    try
                    {
                        dr = part.AttachFile(DrawingType.Document, filePath, false, true);
                        diag["attach_doc"] = dr != null ? "ok" : "null";
                        if (dr != null) { attachMethod = "attach_doc"; _uploadCurrentObjectId = dr.ObjectID; }
                    }
                    catch (Exception ex)
                    {
                        diag["attach_doc_err"] = ex.Message;
                    }
                }
            }
            CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase lowMaster = null;
            if (dr == null)
            {
                try
                {
                    var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
                    var session = engine != null ? engine.CurSession : null;
                    if (session == null)
                    {
                        diag["low_session"] = "unavailable";
                    }
                    else
                    {
                        var store = session.ObjectStore;
                        var partObj = store.RetrieveObject((short)1019, parentId)
                                      as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
                        if (partObj == null)
                        {
                            diag["low_part"] = "null";
                        }
                        else
                        {
                            var dm = session.DocumentManager;
                            string targetClass = GetClassNameByID(session,
                                drawingType == (int)DrawingType.DrawingCAPP ? (short)1003 : (short)1002);
                            var classList = new List<string>();
                            if (!string.IsNullOrEmpty(targetClass)) classList.Add(targetClass);
                            classList.Add("Document");
                            classList.Add("Drawing");
                            classList.Add("CappDocument");
                            string[] classes = classList.ToArray();
                            foreach (var cls in classes)
                            {
                                try
                                {
                                    var master = dm.InputFileToPart(
                                        partObj, cls, filePath, false, false,
                                        CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.EnumTransactionMode.RequireNew);
                                    if (master != null)
                                    {
                                        diag["low_ok_class"] = cls;
                                        attachMethod = "low:" + cls;
                                        lowMaster = master as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase;
                                        if (lowMaster != null) _uploadCurrentObjectId = lowMaster.ObjectId;
                                        break;
                                    }
                                }
                                catch (Exception ex)
                                {
                                    diag["low_" + cls + "_err"] = ex.Message;
                                }
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    diag["low_err"] = ex.Message;
                }
            }
            if (dr == null && lowMaster != null)
            {
                try
                {
                    if (!string.IsNullOrEmpty(name))
                    {
                        lowMaster.SetValue("name", name);
                        lowMaster.Update();
                    }
                    if (!string.IsNullOrEmpty(drawNo))
                    {
                        try { lowMaster.SetValue("DrawNo", drawNo); lowMaster.Update(); } catch { }
                    }
                }
                catch { }
                return Ok(new Dictionary<string, object>
                {
                    { "object_id", lowMaster.ObjectId },
                    { "attached", true },
                    { "low_class", lowMaster.ClassName },
                    { "class_id", ClassIdByName(lowMaster.ClassName) },
                    { "class_name", lowMaster.ClassName },
                    { "method", attachMethod },
                    { "diag", diag }
                });
            }
            if (dr == null)
            {
                if (existingDuplicate != null)
                {
                    try { existingDuplicate.Retrieve(); } catch { }
                    int beforeCnt = diag.ContainsKey("before_history_count") ? Convert.ToInt32(diag["before_history_count"]) : 0;
                    string beforeItr = diag.ContainsKey("before_iterator_no") ? Convert.ToString(diag["before_iterator_no"]) : "";
                    int afterCnt = 0;
                    string afterItr = "";
                    try { afterCnt = (existingDuplicate.GetHistoryDrawings() ?? new IEAPDrawingIteratorEntity[0]).Length; } catch { }
                    try { afterItr = existingDuplicate.IteratorNo ?? ""; } catch { }
                    diag["after_history_count"] = afterCnt;
                    diag["after_iterator_no"] = afterItr;
                    if (afterCnt > beforeCnt || (!string.IsNullOrEmpty(afterItr) && afterItr != beforeItr))
                    {
                        return Ok(new Dictionary<string, object>
                        {
                            { "object_id", existingDuplicate.ObjectID },
                            { "name", existingDuplicate.Name },
                            { "file_name", existingDuplicate.FileName },
                            { "draw_no", existingDuplicate.DrawNo },
                            { "iterator_no", afterItr },
                            { "class_id", existingDuplicate.ClassID },
                            { "class_name", GetClassNameByID(sess, existingDuplicate.ClassID) },
                            { "attached", true },
                            { "new_version", true },
                            { "method", "append_iteration" },
                            { "diag", diag }
                        });
                    }
                    return Err("追加新版次失败：版次未增加（当前版次 " + afterItr + "）");
                }
                return Ok(new Dictionary<string, object> { { "diag", diag }, { "attached", false } });
            }
            if (diag.Count > 0)
            {
                if (!string.IsNullOrEmpty(drawNo)) { try { dr.DrawNo = drawNo; } catch { } }
                if (!string.IsNullOrEmpty(name)) { try { dr.Name = name; } catch { } }
                try { dr.Update(); } catch { }
                return Ok(new Dictionary<string, object>
                {
                    { "object_id", dr.ObjectID },
                    { "draw_no", dr.DrawNo },
                    { "name", dr.Name },
                    { "file_name", dr.FileName },
                    { "state_ext", dr.StateExt },
                    { "attached", true },
                    { "class_id", dr.ClassID },
                    { "class_name", GetClassNameByID(sess, dr.ClassID) },
                    { "method", attachMethod },
                    { "diag", diag }
                });
            }
        }
        else return Err("unsupported parent_kind: " + parentKind);
        if (dr == null) return Err("AttachFile returned null");
        if (!string.IsNullOrEmpty(drawNo)) { try { dr.DrawNo = drawNo; } catch { } }
        if (!string.IsNullOrEmpty(name)) { try { dr.Name = name; } catch { } }
        try { dr.Update(); } catch { }
        return Ok(new Dictionary<string, object>
        {
            { "object_id", dr.ObjectID },
            { "draw_no", dr.DrawNo },
            { "name", dr.Name },
            { "file_name", dr.FileName },
            { "state_ext", dr.StateExt },
            { "attached", true },
            { "class_id", dr.ClassID }
        });
    }

    // 重复图号 → 在当前文档上新增版本/版次。
    // 账号无“反审”（NewRelease）权限，且只处理未发布文件：
    // 流程 = CheckOut（未发布可直接检出）→ 用新 PDF 覆盖检出文件 → CheckIn（生成新版次/版本）。
    // 若文档已发布/被他人检出，CheckOut 会失败，上层回退“复用已有文档”。
    static Dictionary<string, object> AddNewVersion(IEAPDrawingEntity dr, string newFile, Dictionary<string, object> diag)
    {
        var steps = new Dictionary<string, object>();
        diag["new_version_steps"] = steps;
        try
        {
            try { diag["state_ext"] = dr.StateExt; } catch { }
            try { diag["iterator_before"] = dr.IteratorNo; } catch { }
            try { diag["history_before"] = (dr.GetHistoryDrawings() ?? new IEAPDrawingIteratorEntity[0]).Length; } catch { }

            string checkoutPath = null;
            try { checkoutPath = dr.CheckOut(); } catch (Exception ex) { steps["checkout_err"] = ex.Message; }
            if (string.IsNullOrEmpty(checkoutPath))
            {
                // 检出没给路径：尝试显式下载到临时目录
                try
                {
                    string tmp = Path.Combine(Path.GetTempPath(), "caxa_uploader_" + Environment.TickCount);
                    Directory.CreateDirectory(tmp);
                    if (dr.DownLoadFile(tmp, false))
                    {
                        string[] files = Directory.GetFiles(tmp);
                        if (files.Length == 1)
                        {
                            checkoutPath = files[0];
                        }
                        else if (files.Length > 0)
                        {
                            string baseName = Path.GetFileName(newFile);
                            checkoutPath = Array.Find(files, f =>
                                string.Equals(Path.GetFileName(f), baseName, StringComparison.OrdinalIgnoreCase)) ?? files[0];
                        }
                    }
                }
                catch (Exception ex)
                {
                    steps["download_err"] = ex.Message;
                }
            }
            steps["checkout_path"] = checkoutPath ?? "";

            if (!string.IsNullOrEmpty(checkoutPath))
            {
                try
                {
                    File.Copy(newFile, checkoutPath, true);
                    steps["copy"] = true;
                }
                catch (Exception ex)
                {
                    steps["copy_err"] = ex.Message;
                    diag["new_version_copy_failed"] = true;
                }
            }
            else
            {
                diag["new_version_copy_failed"] = true;
            }

            bool ci = false;
            if (string.IsNullOrEmpty(checkoutPath) || diag.ContainsKey("new_version_copy_failed"))
            {
                // 没有可覆盖的检出文件或覆盖失败：绝不检入，避免把旧内容当新版次。
                steps["skip_checkin"] = "copy_failed";
                try { dr.UnDoCheckOut(); steps["undo_checkout"] = true; } catch { }
            }
            else
            {
                try { ci = dr.CheckIn(); } catch (Exception ex) { steps["checkin_err"] = ex.Message; }
                steps["checkin"] = ci;
                if (!ci)
                {
                    try { dr.UnDoCheckOut(); steps["undo_checkout"] = true; } catch { }
                }
            }
            try { diag["iterator_no"] = dr.IteratorNo; } catch { }
            try { diag["history_count"] = (dr.GetHistoryDrawings() ?? new IEAPDrawingIteratorEntity[0]).Length; } catch { }
            return ci ? steps : null;
        }
        catch (Exception ex)
        {
            steps["fatal"] = ex.Message;
            return null;
        }
    }

    // 列出某零件“图纸”页里的所有文档
    static Dictionary<string, object> CmdPartDrawings(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int pid = Convert.ToInt32(a["part_id"]);
        IEAPPartEntity part = _svc.GetPart(pid);
        if (part == null) return Err("part not found: " + pid);
        var list = new List<object>();
        try
        {
            foreach (var comp in part.GetAllMaterDrawing() ?? new CompositeEntity[0])
            {
                IEAPDrawingEntity de = comp.Entity as IEAPDrawingEntity;
                if (de == null) continue;
                var d = new Dictionary<string, object>();
                try { d["object_id"] = de.ObjectID; } catch { }
                try { d["name"] = de.Name; } catch { }
                try { d["draw_no"] = de.DrawNo; } catch { }
                try { d["state"] = de.StateExt; } catch { }
                try { d["file_name"] = de.FileName; } catch { }
                try { d["iterator_no"] = de.IteratorNo; } catch { }
                try { d["history_count"] = (de.GetHistoryDrawings() ?? new IEAPDrawingIteratorEntity[0]).Length; } catch { }
                list.Add(d);
            }
        }
        catch (Exception ex)
        {
            return Err("list drawings error: " + ex.Message);
        }
        return Ok(new Dictionary<string, object> { { "count", list.Count }, { "drawings", list } });
    }

    // 改零件：图号 / 名称（高层属性 + 低层 SetValue 兜底）
    static Dictionary<string, object> CmdSetPart(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int id = Convert.ToInt32(a["object_id"]);
        string identity = a.ContainsKey("identity") ? Convert.ToString(a["identity"]) : null;
        string name = a.ContainsKey("name") ? Convert.ToString(a["name"]) : null;
        IEAPPartEntity pt = _svc.GetPart(id);
        if (pt == null) return Err("part not found: " + id);
        var res = new Dictionary<string, object>();
        if (!string.IsNullOrEmpty(name))
        {
            try { pt.Name = name; res["name"] = name; }
            catch (Exception ex) { res["name_err"] = ex.Message; }
        }
        if (!string.IsNullOrEmpty(identity))
        {
            try
            {
                var prop = pt.GetType().GetProperty("IdentityNo");
                if (prop != null && prop.CanWrite)
                {
                    prop.SetValue(pt, identity, null);
                    res["identity_high"] = identity;
                }
                else
                {
                    res["identity_high"] = "not writable";
                }
            }
            catch (Exception ex) { res["identity_high_err"] = ex.Message; }
        }
        try { pt.Update(); res["updated"] = true; }
        catch (Exception ex) { res["update_err"] = ex.Message; }
        if (!string.IsNullOrEmpty(identity))
        {
            try
            {
                var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
                var session = engine != null ? engine.CurSession : null;
                var store = session != null ? session.ObjectStore : null;
                var obj = store != null
                    ? store.RetrieveObject((short)1019, id)
                      as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase
                    : null;
                if (obj != null)
                {
                    obj.SetValue("IdentityNo", identity);
                    obj.Update();
                    res["identity_low"] = identity;
                }
            }
            catch (Exception ex) { res["identity_low_err"] = ex.Message; }
        }
        var after = _svc.GetPart(id);
        if (after != null)
        {
            res["identity_now"] = after.IdentityNo;
            res["name_now"] = after.Name;
        }
        return Ok(res);
    }

    // 删除图纸（文档）：_svc.GetDrawing(class, id).Delete()
    static Dictionary<string, object> CmdDeleteDrawing(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int id = Convert.ToInt32(a["object_id"]);
        short cls = a.ContainsKey("class_id") ? Convert.ToInt16(a["class_id"]) : (short)1002;
        try
        {
            IEAPDrawingEntity dr = _svc.GetDrawing(cls, id);
            if (dr == null) return Err("drawing not found: " + id);
            var res = new Dictionary<string, object>();
            try { res["name"] = dr.Name; } catch { }
            try { res["iterator_no"] = dr.IteratorNo; } catch { }
            try { res["exists_before"] = dr.Exists(); } catch { }
            _deleteDialogAutoConfirm = true;
            try { dr.Delete(); }
            finally { _deleteDialogAutoConfirm = false; }
            try
            {
                var after = _svc.GetDrawing(cls, id);
                res["get_after"] = after != null;
                if (after != null)
                {
                    try { res["exists_after"] = after.Exists(); } catch { }
                }
                res["deleted"] = after == null;
            }
            catch (Exception ex2)
            {
                res["after_err"] = ex2.Message;
                res["deleted"] = true;
            }
            return Ok(res);
        }
        catch (Exception ex)
        {
            return Err("delete failed: " + ex.Message);
        }
    }

    // 改图纸（文档）的名称 / 图号：低层对象属性 + Update
    static Dictionary<string, object> CmdSetDrawing(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int id = Convert.ToInt32(a["object_id"]);
        string name = a.ContainsKey("name") ? Convert.ToString(a["name"]) : null;
        string drawNo = a.ContainsKey("draw_no") ? Convert.ToString(a["draw_no"]) : null;
        try
        {
            var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
            var session = engine != null ? engine.CurSession : null;
            if (session == null) return Err("session unavailable");
            var store = session.ObjectStore;
            var obj = store.RetrieveObject((short)1002, id)
                      as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase;
            if (obj == null) return Err("drawing object not found: " + id);
            var res = new Dictionary<string, object>();
            if (!string.IsNullOrEmpty(name))
            {
                obj.SetValue("name", name);
                res["name"] = name;
            }
            if (!string.IsNullOrEmpty(drawNo))
            {
                try { obj.SetValue("DrawNo", drawNo); res["draw_no"] = drawNo; }
                catch (Exception ex) { res["draw_no_err"] = ex.Message; }
            }
            obj.Update();
            res["class"] = obj.ClassName;
            res["updated"] = true;
            return Ok(res);
        }
        catch (Exception ex)
        {
            return Err("set drawing error: " + ex.Message);
        }
    }

    static Dictionary<string, object> CmdNewRelease(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int id = Convert.ToInt32(a["drawing_id"]);
        IEAPDrawingEntity dr = FindDrawingById(id);
        if (dr == null) return Err("drawing not found");
        bool ok = dr.NewRelease();
        return ok ? Ok() : Err("NewRelease returned false");
    }

    static Dictionary<string, object> CmdRelease(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int id = Convert.ToInt32(a["drawing_id"]);
        short cls = a.ContainsKey("class_id") ? Convert.ToInt16(a["class_id"]) : (short)1002;
        IEAPDrawingEntity dr = _svc.GetDrawing(cls, id);
        if (dr == null) dr = FindDrawingById(id);
        if (dr == null) return Err("drawing not found: " + id);
        var result = new Dictionary<string, object>();
        try { result["object_id"] = dr.ObjectID; } catch { }
        try { result["name"] = dr.Name; } catch { }
        try { result["iterator_no"] = dr.IteratorNo; } catch { }
        try { result["state_before"] = dr.StateExt; } catch { }
        bool ok = dr.Release();
        result["released"] = ok;
        try { dr.Retrieve(); } catch { }
        try { result["iterator_no_after"] = dr.IteratorNo; } catch { }
        try { result["state_after"] = dr.StateExt; } catch { }
        return ok ? Ok(result) : Err("Release returned false");
    }

    static Dictionary<string, object> CmdStartWorkflow(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        if (!a.ContainsKey("drawing_ids") || a["drawing_ids"] == null)
            return Err("drawing_ids is required");

        var drawingIds = new List<int>();
        var values = a["drawing_ids"] as System.Collections.IEnumerable;
        if (values != null)
        {
            foreach (object value in values)
                drawingIds.Add(Convert.ToInt32(value));
        }
        if (drawingIds.Count == 0) return Err("drawing_ids is empty");
        var uniqueDrawingIds = new List<int>();
        var seenDrawingIds = new HashSet<int>();
        foreach (int drawingId in drawingIds)
        {
            if (seenDrawingIds.Add(drawingId)) uniqueDrawingIds.Add(drawingId);
        }

        uint templateId = a.ContainsKey("template_id")
            ? Convert.ToUInt32(a["template_id"]) : 1u;
        uint templateInstanceId = a.ContainsKey("template_instance_id")
            ? Convert.ToUInt32(a["template_instance_id"]) : 31u;
        string processDefId = a.ContainsKey("process_def_id")
            ? Convert.ToString(a["process_def_id"]) : "2";
        string activityDefId = a.ContainsKey("activity_def_id")
            ? Convert.ToString(a["activity_def_id"]) : "4";
        uint performerId = a.ContainsKey("performer_id")
            ? Convert.ToUInt32(a["performer_id"]) : 66u;
        string processName = a.ContainsKey("process_name")
            ? Convert.ToString(a["process_name"]) : "";
        string processDescription = a.ContainsKey("process_description")
            ? Convert.ToString(a["process_description"]) : "";
        string processNotify = a.ContainsKey("process_notify")
            ? Convert.ToString(a["process_notify"]) : "";
        bool execute = a.ContainsKey("execute") && Convert.ToBoolean(a["execute"]);

        var drawings = new List<Dictionary<string, object>>();
        foreach (int drawingId in uniqueDrawingIds)
        {
            IEAPDrawingEntity drawing = _svc.GetDrawing((short)1002, drawingId);
            if (drawing == null) drawing = FindDrawingById(drawingId);
            if (drawing == null) return Err("drawing not found: " + drawingId);
            var item = new Dictionary<string, object>
            {
                { "object_id", drawing.ObjectID }
            };
            try { item["name"] = drawing.Name; } catch { }
            try { item["state"] = drawing.StateExt; } catch { }
            try { item["iterator_no"] = drawing.IteratorNo; } catch { }
            drawings.Add(item);
        }

        var result = new Dictionary<string, object>
        {
            { "dry_run", !execute },
            { "template_object_id", templateId },
            { "template_instance_id", templateInstanceId },
            { "process_def_id", processDefId },
            { "activity_def_id", activityDefId },
            { "performer_id", performerId },
            { "document_link_class_id", 3305 },
            { "document_ids", uniqueDrawingIds },
            { "drawings", drawings }
        };
        if (!execute) return Ok(result);

        if (!a.ContainsKey("server_ip") || !a.ContainsKey("server_port") || !a.ContainsKey("user_id"))
            return Err("server_ip, server_port, user_id are required when execute=true");

        Interop.EAPLib.CxDalWfConnectionClass connection = null;
        Interop.EAPLib.CxDalWfProcessClass process = null;
        uint connectionId = 0;
        uint processId = 0;
        string stage = "connect";
        // 流程 create 成功后的任何失败都必须带上已建流程 ID 与阶段，
        // 供上层保存为“待核实”，不能按普通失败重放。
        Func<string, Dictionary<string, object>> fail = msg =>
        {
            var resp = Err(msg);
            resp["stage"] = stage;
            resp["partial"] = processId != 0;
            if (processId != 0) resp["created_process_instance_id"] = processId;
            resp["process_instance_id"] = processId;
            resp["result"] = result;
            return resp;
        };
        try
        {
            var serverInfo = new Interop.EAPLib.CxServerInfoClass
            {
                server_ip = Convert.ToString(a["server_ip"]),
                server_port = Convert.ToUInt32(a["server_port"]),
                server_name = "Workflow"
            };
            connection = new Interop.EAPLib.CxDalWfConnectionClass();
            connection.server_type = (int)Interop.EAPLib.PLMSERVERTYPE.PLM_SERVERTYPE_WORKFLOWSERVER;
            connection.server_info = serverInfo;
            stage = "connect";
            connectionId = connection.connect(Convert.ToUInt32(a["user_id"]));
            connection.connection_id = connectionId;
            if (connectionId == 0) return fail("workflow connect returned 0");

            stage = "create";
            process = new Interop.EAPLib.CxDalWfProcessClass();
            process.server_type = (int)Interop.EAPLib.PLMSERVERTYPE.PLM_SERVERTYPE_WORKFLOWSERVER;
            process.server_info = serverInfo;
            process.connection_id = connectionId;
            processId = process.create(
                templateId,
                templateInstanceId,
                processDefId,
                processName,
                processDescription,
                processNotify);
            result["workflow_connection_id"] = connectionId;
            result["process_instance_id"] = processId;
            result["stage"] = stage;
            if (processId == 0) return fail("workflow create returned 0");

            stage = "retrieve_process";
            var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
            var session = engine != null ? engine.CurSession : null;
            if (session == null) return fail("session unavailable");
            var store = session.ObjectStore;
            var processObject = store.RetrieveObject((short)2466, (int)processId)
                as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
            if (processObject == null) return fail("created workflow object not retrievable: " + processId);

            stage = "link_documents";
            var linkIds = new List<int>();
            foreach (int drawingId in uniqueDrawingIds)
            {
                var drawingObject = store.RetrieveObject((short)1002, drawingId)
                    as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
                if (drawingObject == null)
                {
                    result["failed_drawing_id"] = drawingId;
                    result["document_link_ids"] = linkIds;
                    return fail("drawing object not retrievable: " + drawingId);
                }
                var link = store.NewOneLevelLink(3305, processObject, drawingObject, null);
                link.Insert();
                try { linkIds.Add(link.ObjectId); } catch { }
            }
            result["document_link_ids"] = linkIds;

            stage = "designate_performer";
            process.designate_dynamic_performer(
                processId, processDefId, activityDefId, 1,
                performerId.ToString());
            stage = "start";
            process.start(processId);

            // A successful start call is not enough evidence: the process may
            // still be OPEN/NOT_STARTED, and document links may have been lost.
            // Read back the live WFProcessInst state and linked documents.
            stage = "verify_process";
            string processState = "";
            string processStartTime = "";
            var linkedDocumentIds = new List<int>();
            var expectedDocuments = new HashSet<int>(uniqueDrawingIds);
            string verifyError = "";
            bool verified = false;
            for (int attempt = 0; attempt < 10; attempt++)
            {
                if (attempt > 0) System.Threading.Thread.Sleep(200);
                linkedDocumentIds.Clear();
                try
                {
                    var verifiedObject = store.RetrieveObject((short)2466, (int)processId)
                        as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
                    if (verifiedObject == null)
                    {
                        verifyError = "created workflow object disappeared";
                        continue;
                    }
                    try { processState = verifiedObject.GetValueAsString("state") ?? ""; }
                    catch (Exception ex) { verifyError = "state read failed: " + ex.Message; }
                    try { processStartTime = verifiedObject.GetValueAsString("start_time") ?? ""; }
                    catch { processStartTime = ""; }
                    var related = verifiedObject.RetrieveDirectChildren("WFInstanceWithDocument")
                        as System.Collections.IEnumerable;
                    if (related != null)
                    {
                        foreach (object raw in related)
                        {
                            var doc = raw as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObjectBase;
                            if (doc != null) linkedDocumentIds.Add(doc.ObjectId);
                        }
                    }
                    var actualDocuments = new HashSet<int>(linkedDocumentIds);
                    if (processState == "2" &&
                        actualDocuments.SetEquals(expectedDocuments))
                    {
                        verified = true;
                        verifyError = "";
                        break;
                    }
                    verifyError = "state=" + processState
                        + ", linked=" + linkedDocumentIds.Count
                        + "/" + uniqueDrawingIds.Count;
                }
                catch (Exception ex)
                {
                    verifyError = ex.Message;
                }
            }
            result["process_state"] = processState;
            result["process_start_time"] = processStartTime;
            result["linked_document_ids"] = linkedDocumentIds;
            result["state_verified"] = verified;
            result["documents_verified"] = verified;
            if (!verified)
            {
                return fail("流程启动后回读未通过（" + verifyError + "）");
            }
            result["started"] = true;
            result["stage"] = "started";
            return Ok(result);
        }
        catch (Exception ex)
        {
            var errResp = Err("start_workflow 失败: " + ex.Message);
            errResp["detail"] = ex.ToString();
            errResp["stage"] = stage;
            errResp["partial"] = processId != 0;
            if (processId != 0) errResp["created_process_instance_id"] = processId;
            errResp["process_instance_id"] = processId;
            errResp["result"] = result;
            return errResp;
        }
        finally
        {
            if (connection != null && connectionId != 0)
            {
                try { connection.disconnect(connectionId); } catch { }
            }
        }
    }

    static Dictionary<string, object> CmdLinkPart(Dictionary<string, object> a)
    {
        if (_svc == null || !_svc.HasLogin) return Err("not logged in");
        int parentId = Convert.ToInt32(a["parent_id"]);
        int childId = Convert.ToInt32(a["child_id"]);
        bool reference = a.ContainsKey("reference") && Convert.ToBoolean(a["reference"]);
        bool productLink = a.ContainsKey("product_link") && Convert.ToBoolean(a["product_link"]);
        int quantity = a.ContainsKey("quantity") ? Convert.ToInt32(a["quantity"]) : 1;
        if (productLink)
        {
            IEAPProductEntity prod = _svc.GetProduct(parentId);
            IEAPPartEntity part = _svc.GetPart(childId);
            if (prod == null || part == null) return Err("product/part not found");
            IEAPPrd2PartEntity cond = _svc.NewPrd2Part();
            try { cond.Reference = reference; } catch { }
            IEAPPrd2PartEntity link = prod.LinkRelatedPart(part, cond);
            if (link != null)
            {
                return Ok(new Dictionary<string, object> { { "link", "product_part" }, { "link_id", link.ObjectID } });
            }
            // 高层失败：改用低层 NewOneLevelLink 建产品-零件链接
            try
            {
                var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
                var session = engine != null ? engine.CurSession : null;
                if (session == null) return Err("LinkRelatedPart returned null（会话不可用）");
                short linkClassId = 0;
                try
                {
                    var lc = session.MetaInfo.GetLinkClass(1012, 1019);
                    var lb = lc as CAXA.A5.EAP.CommonLibrary.MetaInfo.ICaxClassBase;
                    if (lb != null) linkClassId = lb.ClassId;
                }
                catch { }
                if (linkClassId == 0)
                {
                    foreach (var p in GetAllProductsList())
                    {
                        try
                        {
                            var links = p.RelatedRootParts((IEAPPrd2PartEntity)null);
                            if (links != null && links.Length > 0)
                            {
                                linkClassId = links[0].ClassID;
                                break;
                            }
                        }
                        catch { }
                    }
                }
                if (linkClassId == 0) return Err("LinkRelatedPart returned null（无法确定产品-零件链接类）");
                var store = session.ObjectStore;
                var prodObj = store.RetrieveObject(1012, parentId)
                               as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
                var partObj = store.RetrieveObject(1019, childId)
                              as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
                if (prodObj == null || partObj == null) return Err("LinkRelatedPart returned null（对象不可用）");
                var newLink = store.NewOneLevelLink(linkClassId, prodObj, partObj, null);
                newLink.Insert();
                return Ok(new Dictionary<string, object> { { "link", "product_part_low" }, { "link_class", linkClassId } });
            }
            catch (Exception ex)
            {
                return Err("LinkRelatedPart returned null（低层也失败：" + ex.Message + "）");
            }
        }
        IEAPPartEntity parent = _svc.GetPart(parentId);
        IEAPPartEntity child = _svc.GetPart(childId);
        if (parent == null || child == null) return Err("part not found");
        IEAPPart2PartEntity cond2 = null;
        try { cond2 = _svc.NewPart2Part(); } catch { }
        if (cond2 != null)
        {
            try { cond2.Reference = reference; } catch { }
            try { cond2.AssemblyQuantity = quantity; } catch { }
            try
            {
                IEAPPart2PartEntity link2 = parent.LinkSubPart(child, cond2);
                if (link2 != null)
                {
                    return Ok(new Dictionary<string, object>
                    {
                        { "link", "part_part" },
                        { "link_id", link2.ObjectID },
                        { "reference", link2.Reference }
                    });
                }
            }
            catch (Exception)
            {
                // 高层异常：记录后继续走低层后备
            }
        }
        // 低层后备：Part2Part 链接类 1037
        try
        {
            var engine = CAXA.A5.EAP.CommonLibrary.EAPLibEngine.Instance;
            var session = engine != null ? engine.CurSession : null;
            if (session == null) return Err("part link: 会话不可用");
            var store = session.ObjectStore;
            var parentObj = store.RetrieveObject((short)1019, parentId)
                            as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
            var childObj = store.RetrieveObject((short)1019, childId)
                           as CAXA.A5.EAP.CommonLibrary.Session.ObjectStore.ICaxObject;
            if (parentObj == null || childObj == null) return Err("part link: 对象不可用");
            var newLink = store.NewOneLevelLink(1037, parentObj, childObj, null);
            try { newLink.SetValue("Reference", reference); } catch { }
            try { newLink.SetValue("AssemblyQuantity", quantity); } catch { }
            newLink.Insert();
            return Ok(new Dictionary<string, object>
            {
                { "link", "part_part_low" },
                { "reference", reference },
                { "quantity", quantity }
            });
        }
        catch (Exception ex)
        {
            return Err("part link 低层失败：" + ex.Message);
        }
    }

    static int Main(string[] args)
    {
        try
        {
            StartDialogAutoConfirmer();
            Console.OutputEncoding = new UTF8Encoding(false);
            Console.InputEncoding = new UTF8Encoding(false);
            string line;
            while ((line = Console.ReadLine()) != null)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                Dictionary<string, object> resp;
                try
                {
                    var req = _json.Deserialize<Dictionary<string, object>>(line);
                    string cmd = Convert.ToString(req["cmd"]);
                    switch (cmd)
                    {
                        case "login": resp = CmdLogin(req); break;
                        case "logout": resp = CmdLogout(); break;
                        case "dump_tree": resp = CmdDumpTree(req); break;
                        case "find": resp = CmdFind(req); break;
                        case "list_products": resp = CmdListProducts(); break;
                        case "dump_product": resp = CmdDumpProduct(req); break;
                        case "find_product": resp = CmdFindProduct(req); break;
                        case "product_root_part": resp = CmdProductRootPart(req); break;
                        case "find_part": resp = CmdFindPart(req); break;
                        case "find_part_in_product": resp = CmdFindPartInProduct(req); break;
                        case "query_parts": resp = CmdQueryParts(req); break;
                        case "query_objects": resp = CmdQueryObjects(req); break;
                        case "query_links": resp = CmdQueryLinks(req); break;
                        case "wf_dal_probe": resp = CmdWorkflowDalProbe(req); break;
                        case "wf_connection_probe": resp = CmdWorkflowConnectionProbe(req); break;
                        case "related_objects": resp = CmdRelatedObjects(req); break;
                        case "class_info": resp = CmdClassInfo(req); break;
                        case "part_info": resp = CmdPartInfo(req); break;
                        case "create_part": resp = CmdCreatePart(req); break;
                        case "reference": resp = CmdReference(req); break;
                        case "upload": resp = CmdUpload(req); break;
                        case "new_release": resp = CmdNewRelease(req); break;
                        case "release_drawing": resp = CmdRelease(req); break;
                        case "start_workflow": resp = CmdStartWorkflow(req); break;
                        case "link_part": resp = CmdLinkPart(req); break;
                        case "part_drawings": resp = CmdPartDrawings(req); break;
                        case "set_drawing": resp = CmdSetDrawing(req); break;
                        case "set_part": resp = CmdSetPart(req); break;
                        case "delete_drawing": resp = CmdDeleteDrawing(req); break;
                        case "dump_windows": resp = CmdDumpWindows(req); break;
                        case "set_auto_confirm": resp = CmdSetAutoConfirm(req); break;
                        default: resp = Err("unknown cmd: " + cmd); break;
                    }
                }
                catch (Exception ex)
                {
                    resp = Err(ex.Message);
                }
                Console.WriteLine(_json.Serialize(resp));
                Console.Out.Flush();
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine(_json.Serialize(Err(ex.ToString())));
            return 1;
        }
        return 0;
    }

    // ---------- 自动确认“导入文件确认对话框” ----------
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern IntPtr FindWindowW(string lpClassName, string lpWindowName);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool EnumChildWindows(IntPtr hWndParent, EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern IntPtr SendMessageTimeoutW(IntPtr hWnd, uint Msg, IntPtr wParam,
                                             System.Text.StringBuilder lParam, uint fuFlags,
                                             uint uTimeout, out IntPtr lpdwResult);
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern IntPtr SendMessageTimeoutPtr(IntPtr hWnd, uint Msg, IntPtr wParam,
                                               IntPtr lParam, uint fuFlags,
                                               uint uTimeout, out IntPtr lpdwResult);
    const uint WM_GETTEXT = 0x000D;
    const uint WM_GETTEXTLENGTH = 0x000E;
    const uint SMTO_ABORTIFHUNG = 0x0002;

    // 带超时地读窗口文本，避免目标线程不响应时卡死（同步 GetWindowText 会挂住）
    static string SafeWindowText(IntPtr h, int maxLen = 300)
    {
        try
        {
            IntPtr len;
            if (SendMessageTimeoutW(h, WM_GETTEXTLENGTH, IntPtr.Zero, null,
                                    SMTO_ABORTIFHUNG, 80, out len) == IntPtr.Zero)
                return "";
            int n = len.ToInt32();
            if (n <= 0) return "";
            if (n > maxLen) n = maxLen;
            var sb = new System.Text.StringBuilder(n + 2);
            IntPtr res;
            if (SendMessageTimeoutW(h, WM_GETTEXT, (IntPtr)(n + 1), sb,
                                    SMTO_ABORTIFHUNG, 80, out res) == IntPtr.Zero)
                return "";
            return sb.ToString();
        }
        catch { return ""; }
    }
    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    static extern int GetWindowTextW(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern IntPtr SendMessageW(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    const uint BM_CLICK = 0x00F5;

    // 诊断用：列出所有顶层窗口及其子控件文本
    static Dictionary<string, object> CmdDumpWindows(Dictionary<string, object> a)
    {
        var list = new List<object>();
        EnumWindows(delegate(IntPtr h, IntPtr l)
        {
            var d = new Dictionary<string, object>();
            var tb = new System.Text.StringBuilder(300);
            GetWindowTextW(h, tb, 300);
            d["hwnd"] = h.ToInt64();
            d["title"] = tb.ToString();
            var kids = new List<string>();
            EnumChildWindows(h, delegate(IntPtr c, IntPtr l2)
            {
                var sb = new System.Text.StringBuilder(200);
                GetWindowTextW(c, sb, 200);
                if (sb.Length > 0) kids.Add(sb.ToString().Trim());
                return true;
            }, IntPtr.Zero);
            d["child_count"] = kids.Count;
            d["children"] = kids.Count > 25 ? kids.GetRange(0, 25) : kids;
            list.Add(d);
            return true;
        }, IntPtr.Zero);
        return Ok(new Dictionary<string, object> { { "count", list.Count }, { "windows", list } });
    }

    // 助手自己发起删除时置 true，用于自动点“是(Y)”，人手删除不受影响
    static volatile bool _deleteDialogAutoConfirm = false;

    // 自动确认的作用域：仅限“本次上传命令执行期间 + 本 helper 进程自己拥有的
    // 导入对话框”。不再按进程名/标题批量白名单，避免点到其他 CAXA 窗口。
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    const uint BM_GETCHECK = 0x00F0;

    static volatile bool _autoEnabled = false;
    static string _autoMode = "";
    static string _autoRequestId = "";
    static DateTime _autoDeadline = DateTime.MinValue;

    static uint CurrentProcessId()
    {
        try { return (uint)System.Diagnostics.Process.GetCurrentProcess().Id; }
        catch { return 0; }
    }

    static Dictionary<string, object> CmdSetAutoConfirm(Dictionary<string, object> a)
    {
        bool enabled = a.ContainsKey("enabled") && Convert.ToBoolean(a["enabled"]);
        if (!enabled)
        {
            _autoEnabled = false;
            _autoMode = "";
            _autoRequestId = "";
            _autoDeadline = DateTime.MinValue;
            ConfirmLog("auto-confirm disabled");
            return Ok(new Dictionary<string, object> { { "enabled", false } });
        }
        string mode = a.ContainsKey("mode") ? Convert.ToString(a["mode"]) : "";
        if (mode != "append_iteration")
        {
            var resp = Err("unsupported auto-confirm mode: " + mode);
            resp["stage"] = "auto_confirm";
            return resp;
        }
        _autoMode = mode;
        _autoRequestId = a.ContainsKey("request_id") ? Convert.ToString(a["request_id"]) : "";
        _autoDeadline = DateTime.Now.AddSeconds(120);
        _autoEnabled = true;
        ConfirmLog("auto-confirm enable request=" + _autoRequestId
                   + " pid=" + CurrentProcessId() + "（仅本进程导入窗口）");
        return Ok(new Dictionary<string, object>
        {
            { "enabled", true },
            { "mode", _autoMode },
            { "request_id", _autoRequestId },
            { "process_id", CurrentProcessId() },
            { "owner_scoped", true }
        });
    }

    static bool IsAutoTargetAllowed(uint pid)
    {
        // 只在本 helper 的 upload 命令执行期间、且窗口属于本进程时允许点击。
        if (!_autoEnabled || !_uploadInProgress) return false;
        if (DateTime.Now > _autoDeadline) return false;
        if (_autoMode != "append_iteration") return false;
        if (pid == 0) return false;
        return pid == CurrentProcessId();
    }

    static string SafeWindowTitle(IntPtr hWnd)
    {
        try
        {
            var tb = new System.Text.StringBuilder(300);
            GetWindowTextW(hWnd, tb, 300);
            return tb.ToString();
        }
        catch { return ""; }
    }

    // 按子控件文本找“对象已经存在提示框”；多处命中或进程不符时一律不点。
    static IntPtr FindDupDialog(out string childTexts, out uint ownerPid)
    {
        IntPtr found = IntPtr.Zero;
        string texts = "";
        uint pid = 0;
        int matches = 0;
        EnumWindows(delegate(IntPtr h, IntPtr l)
        {
            var parts = new List<string>();
            EnumChildWindows(h, delegate(IntPtr c, IntPtr l2)
            {
                string s = SafeWindowText(c, 200);
                if (s.Length > 0) parts.Add(s.Trim());
                return true;
            }, IntPtr.Zero);
            string joined = string.Join(" | ", parts.ToArray());
            if (!joined.Contains("追加新的版次")) return true;
            uint hpid;
            GetWindowThreadProcessId(h, out hpid);
            if (!IsAutoTargetAllowed(hpid)) return true;
            matches++;
            if (matches == 1)
            {
                found = h;
                texts = joined;
                pid = hpid;
            }
            return true;
        }, IntPtr.Zero);
        if (matches != 1)
        {
            found = IntPtr.Zero;
            if (matches > 1) texts = "";
            pid = 0;
        }
        childTexts = texts;
        ownerPid = pid;
        return found;
    }

    static void StartDialogAutoConfirmer()
    {
        ConfirmLog("auto-confirmer thread starting");
        var t = new System.Threading.Thread(() =>
        {
            ConfirmLog("auto-confirmer thread started（默认空闲，等待 set_auto_confirm）");
            IntPtr lastDel = IntPtr.Zero;
            IntPtr lastDup = IntPtr.Zero;
            IntPtr lastImport = IntPtr.Zero;
            int ticks = 0;
            while (true)
            {
                ticks++;
                bool beat = (ticks % 40 == 0);
                try
                {
                    // 0) 删除确认框（仅助手自己发起删除时处理）
                    IntPtr del = FindWindowW(null, "协同管理");
                    if (del != IntPtr.Zero && del != lastDel)
                    {
                        lastDel = del;
                        uint delPid;
                        GetWindowThreadProcessId(del, out delPid);
                        if (_deleteDialogAutoConfirm && delPid == CurrentProcessId())
                        {
                            ConfirmLog("发现删除确认框: " + DescribeChildren(del));
                            bool yes = ClickChildContaining(del, "是");
                            ConfirmLog("删除确认“是”点击=" + yes);
                        }
                    }
                    if (del == IntPtr.Zero) lastDel = IntPtr.Zero;

                    // 1) 重复对象对话框：只在本次请求窗口内、且属于已锁定进程时处理
                    string dupTexts;
                    uint dupPid;
                    IntPtr dup = FindDupDialog(out dupTexts, out dupPid);
                    if (beat)
                    {
                        ConfirmLog("heartbeat ticks=" + ticks + " auto=" + _autoEnabled
                                   + " dup=" + dup
                                   + (dup != IntPtr.Zero ? " pid=" + dupPid + " 控件=" + dupTexts : ""));
                    }
                    if (dup != IntPtr.Zero)
                    {
                        if (dup != lastDup)
                        {
                            lastDup = dup;
                            ConfirmLog("发现重复对象对话框 hwnd=" + dup + " pid=" + dupPid
                                       + " 标题=" + SafeWindowTitle(dup) + " 控件=" + dupTexts);
                            bool selected = SelectOptionAndVerify(dup, "追加新的版次");
                            ConfirmLog("“在现有版本上追加新的版次”选中=" + selected);
                            if (!selected)
                            {
                                // 选项不明或没能确认选中：不点“确定”
                                System.Threading.Thread.Sleep(300);
                                continue;
                            }
                            System.Threading.Thread.Sleep(120);
                            bool all = ClickUniqueChild(dup, "应用到所有");
                            ConfirmLog("“应用到所有”点击=" + all);
                        }
                        bool okBtn = ClickUniqueChild(dup, "确定");
                        ConfirmLog("重复对象对话框“确定”点击=" + okBtn + " hwnd=" + dup);
                    }
                    else
                    {
                        lastDup = IntPtr.Zero;
                    }

                    // 2) “导入文件确认对话框”：同样只在请求作用域内处理
                    IntPtr dlg = FindWindowW(null, "导入文件确认对话框");
                    if (dlg != IntPtr.Zero && dlg != lastImport)
                    {
                        uint dlgPid;
                        GetWindowThreadProcessId(dlg, out dlgPid);
                        if (IsAutoTargetAllowed(dlgPid))
                        {
                            lastImport = dlg;
                            bool okBtn = ClickUniqueChild(dlg, "确定");
                            ConfirmLog("“导入文件确认对话框”确定点击=" + okBtn);
                        }
                    }
                    if (dlg == IntPtr.Zero) lastImport = IntPtr.Zero;
                }
                catch { }
                System.Threading.Thread.Sleep(250);
            }
        });
        t.IsBackground = true;
        t.Start();
    }

    // 选中选项并回读勾选状态；返回 false 时调用方不得点“确定”。
    static bool SelectOptionAndVerify(IntPtr parent, string fragment)
    {
        var matches = new List<IntPtr>();
        EnumChildWindows(parent, (h, l) =>
        {
            string s = SafeWindowText(h, 300);
            if (!string.IsNullOrEmpty(s) && s.Contains(fragment)) matches.Add(h);
            return true;
        }, IntPtr.Zero);
        if (matches.Count != 1) return false;
        IntPtr target = matches[0];
        try
        {
            IntPtr res;
            if (SendMessageTimeoutPtr(target, BM_CLICK, IntPtr.Zero, IntPtr.Zero,
                                      SMTO_ABORTIFHUNG, 500, out res) == IntPtr.Zero)
                return false;
        }
        catch { return false; }
        System.Threading.Thread.Sleep(100);
        try
        {
            IntPtr state;
            if (SendMessageTimeoutPtr(target, BM_GETCHECK, IntPtr.Zero, IntPtr.Zero,
                                      SMTO_ABORTIFHUNG, 300, out state) == IntPtr.Zero)
                return false;
            return state.ToInt64() == 1;
        }
        catch { return false; }
    }

    // 只在唯一命中时点击（“确定/应用到所有”）。
    static bool ClickUniqueChild(IntPtr parent, string fragment)
    {
        var matches = new List<IntPtr>();
        EnumChildWindows(parent, (h, l) =>
        {
            string s = SafeWindowText(h, 300);
            if (!string.IsNullOrEmpty(s) && s.Contains(fragment)) matches.Add(h);
            return true;
        }, IntPtr.Zero);
        if (matches.Count != 1) return false;
        try
        {
            IntPtr res;
            SendMessageTimeoutPtr(matches[0], BM_CLICK, IntPtr.Zero, IntPtr.Zero,
                                  SMTO_ABORTIFHUNG, 500, out res);
        }
        catch { return false; }
        return true;
    }

    // 找到子控件（文本包含指定片段）并点击，返回是否点中
    static bool ClickChildContaining(IntPtr parent, string fragment)
    {
        IntPtr target = IntPtr.Zero;
        EnumChildWindows(parent, (h, l) =>
        {
            string s = SafeWindowText(h, 300);
            if (!string.IsNullOrEmpty(s) && s.Contains(fragment))
            {
                target = h;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        if (target != IntPtr.Zero)
        {
            try
            {
                IntPtr res;
                SendMessageTimeoutPtr(target, BM_CLICK, IntPtr.Zero, IntPtr.Zero,
                                      SMTO_ABORTIFHUNG, 500, out res);
            }
            catch { }
            return true;
        }
        return false;
    }

    // 自动确认线程的简单日志（排查用）
    static readonly object _logLock = new object();
    // 枚举对话框里的可见子控件文本（写日志用）
    static string DescribeChildren(IntPtr parent)
    {
        var parts = new List<string>();
        EnumChildWindows(parent, (h, l) =>
        {
            string s = SafeWindowText(h, 300);
            if (!string.IsNullOrEmpty(s)) parts.Add(s.Trim());
            return true;
        }, IntPtr.Zero);
        return string.Join(" | ", parts.ToArray());
    }

    static void ConfirmLog(string line)
    {
        try
        {
            lock (_logLock)
            {
                File.AppendAllText(
                    Path.Combine(Path.GetTempPath(), "caxa_autoconfirmer.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " " + line + Environment.NewLine,
                    new UTF8Encoding(false));
            }
        }
        catch { }
    }
}
