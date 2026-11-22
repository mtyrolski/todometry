package com.todoistassistant;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** Saved report grouping rules and their interpretation against local project metadata. */
final class ProjectAdjustments {
  private final JSONObject mappings;
  private final JSONArray archivedParents;
  private final JSONObject projectMappings;

  private ProjectAdjustments(
      JSONObject mappings, JSONArray archivedParents, JSONObject projectMappings) {
    this.mappings = mappings == null ? new JSONObject() : mappings;
    this.archivedParents = archivedParents == null ? new JSONArray() : archivedParents;
    this.projectMappings = projectMappings == null ? new JSONObject() : projectMappings;
  }

  static ProjectAdjustments fromSnapshot(JSONObject saved) throws JSONException {
    JSONObject copy = saved == null ? new JSONObject() : new JSONObject(saved.toString());
    return new ProjectAdjustments(
        copy.optJSONObject("mappings"),
        copy.optJSONArray("archivedParents"),
        copy.optJSONObject("projectMappings"));
  }

  JSONObject toJson() throws JSONException {
    return new JSONObject()
        .put("mappings", mappings)
        .put("archivedParents", archivedParents)
        .put("projectMappings", projectMappings);
  }

  static JSONObject parse(String text) throws Exception {
    JSONObject data = new JSONObject(text);
    if (!data.optString("format").equals("todoist-assistant-project-adjustments-v1"))
      throw new IllegalArgumentException("Unexpected file format");
    JSONObject mappings = data.getJSONObject("mappings");
    JSONArray parents = data.getJSONArray("archivedParents");
    JSONObject projectMappings = data.optJSONObject("projectMappings");
    if (data.has("projectMappings") && projectMappings == null)
      throw new IllegalArgumentException("Invalid project mapping");
    if (projectMappings == null) projectMappings = new JSONObject();
    for (var keys = mappings.keys(); keys.hasNext(); ) {
      String key = keys.next();
      if (key.trim().isEmpty()
          || !(mappings.opt(key) instanceof String)
          || mappings.getString(key).trim().isEmpty())
        throw new IllegalArgumentException("Invalid project mapping");
    }
    for (int i = 0; i < parents.length(); i++)
      if (!(parents.opt(i) instanceof String) || parents.getString(i).trim().isEmpty())
        throw new IllegalArgumentException("Invalid archived parent");
    for (var keys = projectMappings.keys(); keys.hasNext(); ) {
      String key = keys.next();
      JSONObject target = projectMappings.optJSONObject(key);
      if (key.trim().isEmpty()
          || target == null
          || !(target.opt("targetId") instanceof String)
          || target.getString("targetId").trim().isEmpty()
          || !(target.opt("targetName") instanceof String)
          || target.getString("targetName").trim().isEmpty())
        throw new IllegalArgumentException("Invalid project mapping");
    }
    return new ProjectAdjustments(mappings, parents, projectMappings).toJson();
  }

  static JSONObject merge(JSONObject existing, JSONObject incoming) throws Exception {
    ProjectAdjustments previous = fromSnapshot(existing);
    JSONObject importedMappings = incoming.getJSONObject("mappings");
    for (var keys = importedMappings.keys(); keys.hasNext(); ) {
      String key = keys.next();
      previous.mappings.put(key, importedMappings.getString(key));
    }
    JSONObject importedIds = incoming.optJSONObject("projectMappings");
    if (importedIds != null)
      for (var keys = importedIds.keys(); keys.hasNext(); ) {
        String key = keys.next();
        previous.projectMappings.put(
            key, new JSONObject(importedIds.getJSONObject(key).toString()));
      }
    Set<String> parents = new LinkedHashSet<>();
    JSONArray importedParents = incoming.getJSONArray("archivedParents");
    Set<String> importedParentNames = new LinkedHashSet<>();
    for (int i = 0; i < importedParents.length(); i++)
      importedParentNames.add(importedParents.getString(i));
    for (int i = 0; i < previous.archivedParents.length(); i++) {
      String parent = previous.archivedParents.getString(i);
      if (!importedMappings.has(parent) || importedParentNames.contains(parent))
        parents.add(parent);
    }
    parents.addAll(importedParentNames);
    return new ProjectAdjustments(
            previous.mappings, new JSONArray(parents), previous.projectMappings)
        .toJson();
  }

  Resolver resolver(Map<String, JSONObject> projects) {
    return new Resolver(projects);
  }

  private static final class RootChoice {
    final String nextId, label;

    RootChoice(String nextId, String label) {
      this.nextId = nextId;
      this.label = label;
    }
  }

  final class Resolver {
    private final Map<String, JSONObject> projects;
    private final Map<String, List<String>> idsByName = new HashMap<>();
    private final Map<String, RootChoice> nameChoices = new HashMap<>();
    private final Map<String, RootChoice> idChoices = new HashMap<>();
    private final Map<String, String> rootNames = new HashMap<>();
    private final Set<String> promoted = new HashSet<>();

    Resolver(Map<String, JSONObject> projects) {
      this.projects = projects;
      for (int i = 0; i < archivedParents.length(); i++) promoted.add(archivedParents.optString(i));
      projects.forEach(
          (id, project) ->
              idsByName
                  .computeIfAbsent(project.optString("name"), ignored -> new ArrayList<>())
                  .add(id));
      mappings
          .keys()
          .forEachRemaining(
              source -> {
                String target = mappings.optString(source, source);
                if (!target.equals(source)) nameChoices.put(source, namedDestination(target));
              });
      projectMappings
          .keys()
          .forEachRemaining(
              source -> {
                JSONObject project = projects.get(source);
                if (project == null) return;
                RootChoice choice = idChoice(project, projectMappings.optJSONObject(source));
                if (choice != null) idChoices.put(source, choice);
              });
    }

    String rootName(String id) {
      return rootNames.computeIfAbsent(id, this::resolve);
    }

    private String resolve(String origin) {
      List<JSONObject> originalAncestry = ancestry(origin);
      Set<String> visited = new HashSet<>();
      String id = origin;
      while (visited.add(id)) {
        RootChoice choice = chooseRoot(id.equals(origin) ? originalAncestry : ancestry(id));
        if (choice.nextId == null) return choice.label;
        id = choice.nextId;
      }
      // Cyclic assignments fall back to the origin's physical or promoted root.
      for (JSONObject project : originalAncestry)
        if (isPromoted(project)) return rootLabel(project);
      return originalAncestry.isEmpty()
          ? "Unknown project"
          : rootLabel(originalAncestry.get(originalAncestry.size() - 1));
    }

    private List<JSONObject> ancestry(String id) {
      List<JSONObject> ancestors = new ArrayList<>();
      Set<String> visited = new HashSet<>();
      while (!id.isEmpty() && visited.add(id)) {
        JSONObject project = projects.get(id);
        if (project == null) break;
        ancestors.add(project);
        id = TodoistClient.parentId(project);
      }
      return ancestors;
    }

    private RootChoice chooseRoot(List<JSONObject> ancestors) {
      String rootName = "Unknown project";
      RootChoice promotedRoot = null;
      RootChoice closerLegacy = null;
      for (JSONObject project : ancestors) {
        rootName = project.optString("name", rootName);
        RootChoice byId = idChoices.get(project.optString("id"));
        // A nearer archived choice overrides an inherited ID; the ID wins on the same node.
        if (byId != null) return closerLegacy == null ? byId : closerLegacy;
        if (!project.optBoolean("is_archived")) continue;
        if (isPromoted(project)) {
          if (promotedRoot == null) promotedRoot = new RootChoice(null, rootLabel(project));
          if (closerLegacy == null) closerLegacy = promotedRoot;
        } else if (closerLegacy == null) {
          closerLegacy = nameChoices.get(project.optString("name"));
        }
      }
      // Without an ID rule, promotions take precedence over any legacy name mapping.
      if (promotedRoot != null) return promotedRoot;
      if (closerLegacy != null) return closerLegacy;
      RootChoice renamed = nameChoices.get(rootName);
      if (renamed != null) return renamed;
      return new RootChoice(
          null, ancestors.isEmpty() ? rootName : rootLabel(ancestors.get(ancestors.size() - 1)));
    }

    private boolean isPromoted(JSONObject project) {
      return project.optBoolean("is_archived") && promoted.contains(project.optString("name"));
    }

    private RootChoice idChoice(JSONObject project, JSONObject target) {
      if (target == null) return null;
      String targetId = target.optString("targetId");
      String targetName = target.optString("targetName");
      if (targetId.isEmpty() || targetName.isEmpty()) return null;
      if (project.optString("id").equals(targetId)) return new RootChoice(null, rootLabel(project));
      if (projects.containsKey(targetId)) return new RootChoice(targetId, null);
      return new RootChoice(null, targetName + " · " + targetId);
    }

    private RootChoice namedDestination(String target) {
      String active = null, archived = null;
      int activeMatches = 0, archivedMatches = 0;
      for (String id : idsByName.getOrDefault(target, Collections.emptyList())) {
        if (projects.get(id).optBoolean("is_archived")) {
          archived = id;
          archivedMatches++;
        } else {
          active = id;
          activeMatches++;
        }
      }
      if (activeMatches == 1) return new RootChoice(active, null);
      if (activeMatches == 0 && archivedMatches == 1) return new RootChoice(archived, null);
      // Keep the configured label when metadata is missing or the name is ambiguous.
      return new RootChoice(null, target);
    }

    private String rootLabel(JSONObject project) {
      String name = project.optString("name", "Unknown project");
      String id = project.optString("id");
      for (String other : idsByName.getOrDefault(name, Collections.emptyList()))
        if (!id.equals(projects.get(other).optString("id"))) return name + " · " + id;
      return name;
    }
  }
}
