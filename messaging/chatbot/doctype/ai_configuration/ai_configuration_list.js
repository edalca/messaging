frappe.listview_settings['AI Configuration'] = {
    onload(listview) {
        listview.page_title = __('AI Profiles');
        listview.set_breadcrumbs();
        listview.set_title();
    }
};
